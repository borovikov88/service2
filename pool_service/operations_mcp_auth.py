"""OAuth 2.1 authorization for isolated Service2 operational actions.

Storage reuses the hardened opaque-token tables introduced for Finance MCP,
but every Diagnostic grant/token is bound to its own resource and scope.  No
Finance MCP authorization decision is reused here.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from pool_service.finance_mcp_auth import (
    CHATGPT_CLIENT_ID_METADATA_URL,
    FinanceMcpConfigurationError,
    issuer_url as finance_issuer_url,
)
from pool_service.models import (
    FinanceMcpAccessToken,
    FinanceMcpAuthorizationCode,
    FinanceMcpClient,
    FinanceMcpGrant,
    FinanceMcpRefreshToken,
    Organization,
)
from pool_service.operations_mcp_policy import can_access_operations_mcp


OPERATIONS_SCOPE = "service2.operations"
OFFLINE_ACCESS_SCOPE = "offline_access"
ORGANIZATION_SCOPE_PREFIX = "service2.operations.organization:"
SUPPORTED_SCOPES = frozenset({OPERATIONS_SCOPE, OFFLINE_ACCESS_SCOPE})
AUTHORIZATION_CODE_GRANT = "authorization_code"
REFRESH_TOKEN_GRANT = "refresh_token"
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class OperationsMcpOAuthError(Exception):
    def __init__(self, error, description, status=400):
        self.error = error
        self.description = description
        self.status = status
        super().__init__(description)


class OperationsMcpConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthenticatedOperationsMcpRequest:
    token: FinanceMcpAccessToken
    grant: FinanceMcpGrant
    principal: object


def _https_url(value, *, field):
    if not isinstance(value, str) or not value:
        raise OperationsMcpConfigurationError(f"{field} must be configured.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise OperationsMcpConfigurationError(
            f"{field} must be an absolute HTTPS URL without query or fragment."
        )
    return urlunsplit((
        parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""
    ))


def _canonical_https_origin(value, *, field):
    if not isinstance(value, str) or not value:
        raise OperationsMcpConfigurationError(f"{field} must be an absolute HTTPS origin.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise OperationsMcpConfigurationError(f"{field} must be an exact HTTPS origin.")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))


def is_enabled():
    return bool(getattr(settings, "ADVISOR_OPERATIONS_MCP_ENABLED", False))


def operations_mcp_allowed_origins():
    configured = getattr(
        settings,
        "ADVISOR_OPERATIONS_MCP_ALLOWED_ORIGINS",
        {"https://chatgpt.com"},
    )
    if isinstance(configured, str):
        values = configured.split(",")
    else:
        try:
            values = list(configured)
        except TypeError as exc:
            raise OperationsMcpConfigurationError(
                "ADVISOR_OPERATIONS_MCP_ALLOWED_ORIGINS must be a collection."
            ) from exc
    result = set()
    for value in values:
        if not isinstance(value, str):
            raise OperationsMcpConfigurationError(
                "ADVISOR_OPERATIONS_MCP_ALLOWED_ORIGINS must contain strings."
            )
        if value.strip():
            result.add(_canonical_https_origin(
                value.strip(), field="ADVISOR_OPERATIONS_MCP_ALLOWED_ORIGINS"
            ))
    return frozenset(result)


def operations_mcp_origin_is_allowed(origin):
    if not origin:
        return True
    try:
        normalized = _canonical_https_origin(origin, field="Origin")
    except OperationsMcpConfigurationError:
        return False
    return normalized in operations_mcp_allowed_origins()


def resource_url():
    configured = getattr(settings, "ADVISOR_OPERATIONS_MCP_RESOURCE_URL", "")
    if not configured:
        site_url = getattr(settings, "SITE_URL", "")
        configured = f"{site_url.rstrip('/')}/mcp/operations" if site_url else ""
    return _https_url(configured, field="ADVISOR_OPERATIONS_MCP_RESOURCE_URL")


def issuer_url():
    """Use the single reviewed authorization-server issuer shared by all MCPs."""
    try:
        return finance_issuer_url()
    except FinanceMcpConfigurationError as exc:
        raise OperationsMcpConfigurationError(
            "Shared MCP authorization issuer is not configured."
        ) from exc


def protected_resource_metadata_url():
    site_url = getattr(settings, "SITE_URL", "")
    if not site_url:
        raise OperationsMcpConfigurationError("SITE_URL must be configured.")
    root = _https_url(site_url, field="SITE_URL")
    return f"{root}/.well-known/oauth-protected-resource/mcp/operations"


def authorization_server_metadata_url():
    return f"{issuer_url()}/.well-known/oauth-authorization-server"


def authorization_endpoint_url():
    return f"{issuer_url()}/oauth/finance/authorize"


def token_endpoint_url():
    return f"{issuer_url()}/oauth/finance/token"


def _positive_setting(name, default, *, minimum, maximum):
    value = getattr(settings, name, default)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise OperationsMcpConfigurationError(f"{name} must be an integer.") from exc
    if not minimum <= parsed <= maximum:
        raise OperationsMcpConfigurationError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return parsed


def access_token_ttl_seconds():
    return _positive_setting(
        "ADVISOR_OPERATIONS_MCP_ACCESS_TOKEN_TTL_SECONDS",
        600,
        minimum=60,
        maximum=3600,
    )


def refresh_token_ttl_seconds():
    return _positive_setting(
        "ADVISOR_OPERATIONS_MCP_REFRESH_TOKEN_TTL_SECONDS",
        2_592_000,
        minimum=3600,
        maximum=7_776_000,
    )


def authorization_code_ttl_seconds():
    return _positive_setting(
        "ADVISOR_OPERATIONS_MCP_AUTHORIZATION_CODE_TTL_SECONDS",
        300,
        minimum=60,
        maximum=900,
    )


def _secret_hash(value):
    if not isinstance(value, str) or not value:
        raise OperationsMcpOAuthError("invalid_request", "Не передан обязательный OAuth параметр.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _random_secret():
    return secrets.token_urlsafe(48)


def _normalize_scopes(value, *, require_read=True):
    if value is None:
        values = {OPERATIONS_SCOPE}
    elif not isinstance(value, str):
        raise OperationsMcpOAuthError("invalid_scope", "Некорректный OAuth scope.")
    else:
        values = {item for item in value.split() if item}
    if not values or not values.issubset(SUPPORTED_SCOPES):
        raise OperationsMcpOAuthError("invalid_scope", "Запрошен неподдерживаемый OAuth scope.")
    if require_read and OPERATIONS_SCOPE not in values:
        raise OperationsMcpOAuthError(
            "invalid_scope", "Для Operations MCP необходим scope service2.operations."
        )
    return sorted(values)


def _valid_redirect_uri(value):
    if not isinstance(value, str):
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.scheme == "https"
        and parsed.netloc
        and not parsed.username
        and not parsed.password
        and not parsed.fragment
    )


def _is_pinned_chatgpt_client(client):
    return bool(
        client
        and client.client_type == FinanceMcpClient.CLIENT_PUBLIC
        and client.client_id == CHATGPT_CLIENT_ID_METADATA_URL
        and isinstance(client.redirect_uris, list)
        and len(client.redirect_uris) == 1
        and all(_valid_redirect_uri(uri) for uri in client.redirect_uris)
        and len(set(client.redirect_uris)) == len(client.redirect_uris)
        and isinstance(client.client_metadata_sha256, str)
        and _SHA256_HEX_RE.fullmatch(client.client_metadata_sha256)
        and client.client_metadata_verified_at is not None
    )


def client_for_authorization(client_id, redirect_uri):
    if not isinstance(client_id, str) or not client_id or len(client_id) > 255:
        raise OperationsMcpOAuthError("invalid_client", "Неизвестный OAuth клиент.", status=401)
    client = FinanceMcpClient.objects.select_related("principal").filter(
        client_id=client_id,
        is_active=True,
        revoked_at__isnull=True,
        principal__is_active=True,
        principal__revoked_at__isnull=True,
    ).first()
    if not _is_pinned_chatgpt_client(client):
        raise OperationsMcpOAuthError("invalid_client", "Неизвестный OAuth клиент.", status=401)
    if not _valid_redirect_uri(redirect_uri) or redirect_uri not in client.redirect_uris:
        raise OperationsMcpOAuthError("invalid_request", "Redirect URI не зарегистрирован.")
    return client


def _validate_pkce_challenge(value):
    if not isinstance(value, str) or not 43 <= len(value) <= 128:
        raise OperationsMcpOAuthError("invalid_request", "Требуется S256 PKCE code_challenge.")
    try:
        base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError) as exc:
        raise OperationsMcpOAuthError("invalid_request", "Некорректный PKCE code_challenge.") from exc
    return value


def _verify_pkce(verifier, challenge):
    if not isinstance(verifier, str) or not 43 <= len(verifier) <= 128:
        return False
    try:
        verifier_bytes = verifier.encode("ascii")
    except UnicodeEncodeError:
        return False
    digest = hashlib.sha256(verifier_bytes).digest()
    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return secrets.compare_digest(expected, challenge)


def target_organization():
    raw = getattr(settings, "ADVISOR_OPERATIONS_MCP_ORGANIZATION_ID", None)
    if isinstance(raw, bool):
        raise OperationsMcpConfigurationError("ADVISOR_OPERATIONS_MCP_ORGANIZATION_ID is invalid.")
    try:
        organization_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise OperationsMcpConfigurationError(
            "ADVISOR_OPERATIONS_MCP_ORGANIZATION_ID is invalid."
        ) from exc
    organization = Organization.objects.filter(pk=organization_id).first()
    if organization is None:
        raise OperationsMcpConfigurationError(
            "ADVISOR_OPERATIONS_MCP_ORGANIZATION_ID does not reference an organization."
        )
    return organization


def principal_has_target_scope(client):
    organization = target_organization()
    return client.principal.organization_scopes.filter(organization=organization).exists()


def _organization_scope(organization_id):
    return f"{ORGANIZATION_SCOPE_PREFIX}{int(organization_id)}"


def _grant_is_bound_to_organization(grant, organization):
    scopes = grant.scopes if isinstance(grant.scopes, list) else []
    return _organization_scope(organization.id) in scopes


def validate_authorization_request(params):
    if not isinstance(params, dict):
        raise OperationsMcpOAuthError("invalid_request", "Некорректный OAuth запрос.")
    if params.get("response_type") != "code":
        raise OperationsMcpOAuthError(
            "unsupported_response_type", "Поддерживается только authorization code."
        )
    state = params.get("state")
    if not isinstance(state, str) or not state or len(state) > 2048:
        raise OperationsMcpOAuthError("invalid_request", "OAuth state обязателен.")
    if params.get("code_challenge_method") != "S256":
        raise OperationsMcpOAuthError("invalid_request", "Требуется PKCE S256.")
    challenge = _validate_pkce_challenge(params.get("code_challenge"))
    resource = params.get("resource")
    if resource != resource_url():
        raise OperationsMcpOAuthError(
            "invalid_target", "OAuth resource не соответствует Operations MCP."
        )
    client = client_for_authorization(params.get("client_id"), params.get("redirect_uri"))
    organization = target_organization()
    if not client.principal.organization_scopes.filter(organization=organization).exists():
        raise OperationsMcpOAuthError("access_denied", "Operations principal не имеет target scope.", status=403)
    return {
        "client": client,
        "state": state,
        "redirect_uri": params["redirect_uri"],
        "resource": resource,
        "scopes": _normalize_scopes(params.get("scope")),
        "code_challenge": challenge,
        "organization_id": organization.id,
    }


def authorization_is_allowed(user, client):
    try:
        organization = target_organization()
    except OperationsMcpConfigurationError:
        return False
    return bool(principal_has_target_scope(client) and can_access_operations_mcp(user, organization))


def scoped_organizations(client):
    organization = target_organization()
    return list(
        client.principal.organization_scopes.select_related("organization")
        .filter(organization=organization)
    )


def _revoke_operations_grants(queryset, *, now, reason):
    grants = list(queryset.select_for_update())
    if not grants:
        return []
    grant_ids = [grant.id for grant in grants]
    FinanceMcpAccessToken.objects.filter(
        grant_id__in=grant_ids, revoked_at__isnull=True
    ).update(revoked_at=now)
    FinanceMcpRefreshToken.objects.filter(
        grant_id__in=grant_ids, revoked_at__isnull=True
    ).update(revoked_at=now)
    FinanceMcpGrant.objects.filter(id__in=grant_ids, revoked_at__isnull=True).update(
        revoked_at=now, revocation_reason=reason[:300]
    )
    return grant_ids


def issue_authorization_code(*, authorization, user):
    client = authorization["client"]
    if not authorization_is_allowed(user, client):
        raise OperationsMcpOAuthError(
            "access_denied", "Недостаточно прав для Operations MCP.", status=403
        )
    now = timezone.now()
    raw_code = _random_secret()
    with transaction.atomic():
        # Multiple ChatGPT connector links may share this public client while
        # remaining independent grants.  Re-authorizing one Diagnostic link
        # must not revoke sibling Diagnostic grants or Finance grants.
        # Explicit revoke and refresh-token replay handling stay grant-scoped.
        organization = target_organization()
        if organization.id != authorization.get("organization_id"):
            raise OperationsMcpOAuthError(
                "access_denied",
                "Целевая организация изменилась после показа согласия.",
                status=403,
            )
        grant_scopes = sorted(
            set(authorization["scopes"]) | {_organization_scope(organization.id)}
        )
        grant = FinanceMcpGrant.objects.create(
            client=client,
            principal=client.principal,
            authorized_by=user,
            scopes=grant_scopes,
            resource=authorization["resource"],
        )
        FinanceMcpAuthorizationCode.objects.create(
            grant=grant,
            code_hash=_secret_hash(raw_code),
            redirect_uri=authorization["redirect_uri"],
            resource=authorization["resource"],
            scopes=authorization["scopes"],
            code_challenge=authorization["code_challenge"],
            expires_at=now + timedelta(seconds=authorization_code_ttl_seconds()),
        )
    return raw_code


def _grant_is_valid(grant, *, now, client, resource):
    if not (
        grant
        and grant.client_id == client.id
        and grant.principal_id == client.principal_id
        and grant.resource == resource
        and grant.revoked_at is None
        and (grant.expires_at is None or grant.expires_at > now)
        and client.is_active
        and client.revoked_at is None
        and client.principal.is_active
        and client.principal.revoked_at is None
        and grant.authorized_by_id
    ):
        return False
    try:
        organization = target_organization()
    except OperationsMcpConfigurationError:
        return False
    return bool(
        _grant_is_bound_to_organization(grant, organization)
        and principal_has_target_scope(client)
        and can_access_operations_mcp(grant.authorized_by, organization)
    )


def _issue_tokens(grant, *, scopes, family_id=None):
    now = timezone.now()
    raw_access = _random_secret()
    raw_refresh = _random_secret() if OFFLINE_ACCESS_SCOPE in scopes else None
    access = FinanceMcpAccessToken.objects.create(
        grant=grant,
        token_hash=_secret_hash(raw_access),
        audience=grant.resource,
        scopes=scopes,
        expires_at=now + timedelta(seconds=access_token_ttl_seconds()),
    )
    refresh = None
    if raw_refresh is not None:
        values = {
            "grant": grant,
            "token_hash": _secret_hash(raw_refresh),
            "scopes": scopes,
            "expires_at": now + timedelta(seconds=refresh_token_ttl_seconds()),
        }
        if family_id is not None:
            values["family_id"] = family_id
        refresh = FinanceMcpRefreshToken.objects.create(**values)
    response = {
        "access_token": raw_access,
        "token_type": "Bearer",
        "expires_in": access_token_ttl_seconds(),
        "scope": " ".join(scopes),
    }
    if raw_refresh is not None:
        response["refresh_token"] = raw_refresh
    return response, refresh


def exchange_authorization_code(params):
    required = ("code", "client_id", "redirect_uri", "code_verifier", "resource")
    if any(not params.get(key) for key in required):
        raise OperationsMcpOAuthError("invalid_request", "Не передан обязательный OAuth параметр.")
    if params.get("resource") != resource_url():
        raise OperationsMcpOAuthError("invalid_target", "OAuth resource не соответствует Operations MCP.")
    client = client_for_authorization(params.get("client_id"), params.get("redirect_uri"))
    now = timezone.now()
    with transaction.atomic():
        code = FinanceMcpAuthorizationCode.objects.select_for_update().select_related(
            "grant__client__principal", "grant__authorized_by"
        ).filter(code_hash=_secret_hash(params.get("code"))).first()
        if code is None or code.used_at is not None or code.expires_at <= now:
            raise OperationsMcpOAuthError("invalid_grant", "Authorization code недействителен.")
        if (
            code.redirect_uri != params.get("redirect_uri")
            or code.resource != params.get("resource")
            or OPERATIONS_SCOPE not in code.scopes
            or not _verify_pkce(params.get("code_verifier"), code.code_challenge)
            or not _grant_is_valid(code.grant, now=now, client=client, resource=code.resource)
        ):
            raise OperationsMcpOAuthError("invalid_grant", "Authorization code недействителен.")
        code.used_at = now
        code.save(update_fields=["used_at"])
        response, _ = _issue_tokens(code.grant, scopes=list(code.scopes))
    return response


def exchange_refresh_token(params):
    required = ("refresh_token", "client_id", "resource")
    if any(not params.get(key) for key in required):
        raise OperationsMcpOAuthError("invalid_request", "Не передан обязательный OAuth параметр.")
    if params.get("resource") != resource_url():
        raise OperationsMcpOAuthError("invalid_target", "OAuth resource не соответствует Operations MCP.")
    client = FinanceMcpClient.objects.select_related("principal").filter(
        client_id=params.get("client_id"),
        is_active=True,
        revoked_at__isnull=True,
        principal__is_active=True,
        principal__revoked_at__isnull=True,
    ).first()
    if not _is_pinned_chatgpt_client(client):
        raise OperationsMcpOAuthError("invalid_client", "Неизвестный OAuth клиент.", status=401)
    requested = _normalize_scopes(params.get("scope")) if params.get("scope") else None
    now = timezone.now()
    invalid = False
    response = None
    with transaction.atomic():
        refresh = FinanceMcpRefreshToken.objects.select_for_update().select_related(
            "grant__client__principal", "grant__authorized_by"
        ).filter(token_hash=_secret_hash(params.get("refresh_token"))).first()
        if refresh is None:
            raise OperationsMcpOAuthError("invalid_grant", "Refresh token недействителен.")
        valid = (
            refresh.used_at is None
            and refresh.revoked_at is None
            and refresh.expires_at > now
            and OPERATIONS_SCOPE in refresh.scopes
            and _grant_is_valid(refresh.grant, now=now, client=client, resource=params.get("resource"))
        )
        if not valid:
            _revoke_operations_grants(
                FinanceMcpGrant.objects.filter(
                    pk=refresh.grant_id,
                    resource=params.get("resource"),
                    revoked_at__isnull=True,
                ),
                now=now,
                reason="operations refresh-token reuse or invalid refresh",
            )
            invalid = True
        else:
            original = set(refresh.scopes)
            scopes = sorted(original if requested is None else set(requested))
            if not set(scopes).issubset(original) or OPERATIONS_SCOPE not in scopes:
                raise OperationsMcpOAuthError("invalid_scope", "Нельзя расширить Operations scope.")
            refresh.used_at = now
            response, replacement = _issue_tokens(
                refresh.grant, scopes=scopes, family_id=refresh.family_id
            )
            refresh.replaced_by = replacement
            refresh.save(update_fields=["used_at", "replaced_by"])
    if invalid:
        raise OperationsMcpOAuthError("invalid_grant", "Refresh token недействителен.")
    return response


def exchange_token(params):
    if not isinstance(params, dict):
        raise OperationsMcpOAuthError("invalid_request", "Некорректный OAuth запрос.")
    grant_type = params.get("grant_type")
    if grant_type == AUTHORIZATION_CODE_GRANT:
        return exchange_authorization_code(params)
    if grant_type == REFRESH_TOKEN_GRANT:
        return exchange_refresh_token(params)
    raise OperationsMcpOAuthError(
        "unsupported_grant_type", "Поддерживаются authorization_code и refresh_token."
    )


def authenticate_bearer_header(value):
    if not isinstance(value, str) or not value.startswith("Bearer "):
        raise OperationsMcpOAuthError("invalid_token", "Требуется Bearer access token.", status=401)
    raw_token = value.removeprefix("Bearer ")
    if not raw_token or any(character.isspace() for character in raw_token):
        raise OperationsMcpOAuthError("invalid_token", "Требуется Bearer access token.", status=401)
    now = timezone.now()
    token = FinanceMcpAccessToken.objects.select_related(
        "grant__principal", "grant__client", "grant__authorized_by"
    ).filter(token_hash=_secret_hash(raw_token)).first()
    if token is None:
        raise OperationsMcpOAuthError("invalid_token", "Access token недействителен.", status=401)
    grant = token.grant
    client = grant.client
    if not (
        token.expires_at > now
        and token.revoked_at is None
        and token.audience == resource_url()
        and OPERATIONS_SCOPE in token.scopes
        and OPERATIONS_SCOPE in grant.scopes
        and _grant_is_valid(grant, now=now, client=client, resource=token.audience)
    ):
        raise OperationsMcpOAuthError("invalid_token", "Access token недействителен.", status=401)
    FinanceMcpAccessToken.objects.filter(pk=token.pk).update(last_used_at=now)
    return AuthenticatedOperationsMcpRequest(
        token=token, grant=grant, principal=grant.principal
    )


def revalidate_authenticated_request(authenticated):
    """Lock/re-read mutable token and grant state inside a tool transaction."""
    now = timezone.now()
    token_id = getattr(getattr(authenticated, "token", None), "pk", None)
    grant_id = getattr(getattr(authenticated, "grant", None), "pk", None)
    if not token_id or not grant_id:
        raise PermissionError("operations_authorization_revoked")

    token = FinanceMcpAccessToken.objects.select_for_update().filter(
        pk=token_id,
    ).first()
    grant = (
        FinanceMcpGrant.objects.select_for_update()
        .select_related("client__principal", "principal", "authorized_by")
        .filter(pk=grant_id)
        .first()
    )
    if (
        token is None
        or grant is None
        or token.grant_id != grant.id
        or token.expires_at <= now
        or token.revoked_at is not None
        or token.audience != resource_url()
        or OPERATIONS_SCOPE not in token.scopes
        or OPERATIONS_SCOPE not in grant.scopes
        or not _grant_is_valid(
            grant,
            now=now,
            client=grant.client,
            resource=token.audience,
        )
    ):
        raise PermissionError("operations_authorization_revoked")

    return AuthenticatedOperationsMcpRequest(
        token=token,
        grant=grant,
        principal=grant.principal,
    )


def authorization_redirect_uri(redirect_uri, *, code=None, state=None, error=None, error_description=None):
    parts = urlsplit(redirect_uri)
    query = dict(parse_qsl(parts.query, keep_blank_values=True)) if parts.query else {}
    if code is not None:
        query["code"] = code
    if error is not None:
        query["error"] = error
    if error_description is not None:
        query["error_description"] = error_description
    if state is not None:
        query["state"] = state
    query["iss"] = issuer_url()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def protected_resource_metadata():
    return {
        "resource": resource_url(),
        "authorization_servers": [issuer_url()],
        "scopes_supported": [OPERATIONS_SCOPE],
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata():
    return {
        "issuer": issuer_url(),
        "authorization_endpoint": authorization_endpoint_url(),
        "token_endpoint": token_endpoint_url(),
        "response_types_supported": ["code"],
        "grant_types_supported": [AUTHORIZATION_CODE_GRANT, REFRESH_TOKEN_GRANT],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": [OPERATIONS_SCOPE, OFFLINE_ACCESS_SCOPE],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }


__all__ = [
    "OPERATIONS_SCOPE",
    "ORGANIZATION_SCOPE_PREFIX",
    "OperationsMcpConfigurationError",
    "OperationsMcpOAuthError",
    "authorization_redirect_uri",
    "authorization_server_metadata",
    "authenticate_bearer_header",
    "operations_mcp_origin_is_allowed",
    "exchange_token",
    "is_enabled",
    "issue_authorization_code",
    "protected_resource_metadata",
    "protected_resource_metadata_url",
    "revalidate_authenticated_request",
    "resource_url",
    "scoped_organizations",
    "validate_authorization_request",
]
