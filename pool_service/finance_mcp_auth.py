"""OAuth 2.1 primitives for the protected Service2 finance MCP.

The implementation is deliberately small and self-contained: only a
pre-registered public client is accepted, authorization-code + S256 PKCE is
mandatory, access/refresh/code secrets are opaque random strings and only their
SHA-256 hashes are stored.  This is an OAuth authorization server *and* the
resource server's local token validator; it never delegates finance decisions
to a browser, a URL token, or an MCP request body.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import base64
import hashlib
import json
import re
import secrets
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from pool_service.models import (
    FinanceMcpAccessToken,
    FinanceMcpAuthorizationCode,
    FinanceMcpClient,
    FinanceMcpGrant,
    FinanceMcpPrincipal,
    FinanceMcpRefreshToken,
)
from pool_service.services.finance import can_manage_finance


FINANCE_READ_SCOPE = "finance.read"
OFFLINE_ACCESS_SCOPE = "offline_access"
SUPPORTED_SCOPES = frozenset({FINANCE_READ_SCOPE, OFFLINE_ACCESS_SCOPE})
AUTHORIZATION_CODE_GRANT = "authorization_code"
REFRESH_TOKEN_GRANT = "refresh_token"

# ChatGPT Developer Mode identifies its public OAuth client with a Client ID
# Metadata Document (CIMD), not a locally invented opaque client ID.  Keep the
# only fetch target literal and narrow: provisioning may retrieve this document
# once, while the authorization and token endpoints use only the pinned DB
# registration and never dereference a caller-provided URL.
CHATGPT_CLIENT_ID_METADATA_URL = "https://chatgpt.com/oauth/client.json"
MAX_CHATGPT_CLIENT_METADATA_BYTES = 16 * 1024
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class _NoRedirectHandler(HTTPRedirectHandler):
    """Refuse every 3xx response while checking the fixed CIMD URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FinanceMcpOAuthError(Exception):
    """Safe OAuth error, never containing an input token or secret."""

    def __init__(self, error, description, status=400):
        self.error = error
        self.description = description
        self.status = status
        super().__init__(description)


class FinanceMcpConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuthenticatedFinanceMcpRequest:
    token: FinanceMcpAccessToken
    grant: FinanceMcpGrant
    principal: FinanceMcpPrincipal


def _https_url(value, *, field):
    if not isinstance(value, str) or not value:
        raise FinanceMcpConfigurationError(f"{field} must be configured.")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise FinanceMcpConfigurationError(f"{field} must be an absolute HTTPS URL without query or fragment.")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), "", ""))


def _canonical_https_origin(value, *, field):
    """Normalize an exact browser Origin; paths and credentials are invalid."""
    if not isinstance(value, str) or not value:
        raise FinanceMcpConfigurationError(f"{field} must be an absolute HTTPS origin.")
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
        raise FinanceMcpConfigurationError(f"{field} must be an exact HTTPS origin.")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))


def is_enabled():
    return bool(getattr(settings, "ADVISOR_FINANCE_MCP_ENABLED", False))


def finance_mcp_allowed_origins():
    """Return the explicit browser Origin allowlist for the MCP transport."""
    configured = getattr(
        settings,
        "ADVISOR_FINANCE_MCP_ALLOWED_ORIGINS",
        {"https://chatgpt.com"},
    )
    if isinstance(configured, str):
        values = configured.split(",")
    else:
        try:
            values = list(configured)
        except TypeError as exc:
            raise FinanceMcpConfigurationError(
                "ADVISOR_FINANCE_MCP_ALLOWED_ORIGINS must be a collection of origins."
            ) from exc
    origins = set()
    for value in values:
        if not isinstance(value, str):
            raise FinanceMcpConfigurationError(
                "ADVISOR_FINANCE_MCP_ALLOWED_ORIGINS must contain only origins."
            )
        if not value.strip():
            continue
        origins.add(_canonical_https_origin(
            value.strip(), field="ADVISOR_FINANCE_MCP_ALLOWED_ORIGINS"
        ))
    return frozenset(origins)


def finance_mcp_origin_is_allowed(origin):
    """Allow no-Origin server calls, but fail closed for a present Origin."""
    allowed_origins = finance_mcp_allowed_origins()
    if not origin:
        return True
    try:
        normalized = _canonical_https_origin(origin, field="Origin")
    except FinanceMcpConfigurationError:
        return False
    return normalized in allowed_origins


def resource_url():
    configured = getattr(settings, "ADVISOR_FINANCE_MCP_RESOURCE_URL", "")
    if not configured:
        site_url = getattr(settings, "SITE_URL", "")
        configured = f"{site_url.rstrip('/')}/mcp/finance" if site_url else ""
    return _https_url(configured, field="ADVISOR_FINANCE_MCP_RESOURCE_URL")


def issuer_url():
    configured = getattr(settings, "ADVISOR_FINANCE_MCP_AUTH_ISSUER", "")
    if not configured:
        configured = getattr(settings, "SITE_URL", "")
    return _https_url(configured, field="ADVISOR_FINANCE_MCP_AUTH_ISSUER")


def protected_resource_metadata_url():
    return f"{issuer_url()}/.well-known/oauth-protected-resource/mcp/finance"


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
        raise FinanceMcpConfigurationError(f"{name} must be an integer.") from exc
    if not minimum <= parsed <= maximum:
        raise FinanceMcpConfigurationError(
            f"{name} must be between {minimum} and {maximum}."
        )
    return parsed


def access_token_ttl_seconds():
    return _positive_setting(
        "ADVISOR_FINANCE_MCP_ACCESS_TOKEN_TTL_SECONDS", 600, minimum=60, maximum=3600
    )


def refresh_token_ttl_seconds():
    return _positive_setting(
        "ADVISOR_FINANCE_MCP_REFRESH_TOKEN_TTL_SECONDS",
        2_592_000,
        minimum=3600,
        maximum=7_776_000,
    )


def authorization_code_ttl_seconds():
    return _positive_setting(
        "ADVISOR_FINANCE_MCP_AUTHORIZATION_CODE_TTL_SECONDS", 300, minimum=60, maximum=900
    )


def _secret_hash(value):
    if not isinstance(value, str) or not value:
        raise FinanceMcpOAuthError("invalid_request", "Не передан обязательный OAuth параметр.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _random_secret():
    # URL-safe opaque random token.  Its value never enters a model field or a
    # log call; only the caller receives it once in a no-store response.
    return secrets.token_urlsafe(48)


def _normalize_scopes(value, *, require_finance_read=True):
    if value is None:
        values = {FINANCE_READ_SCOPE}
    elif not isinstance(value, str):
        raise FinanceMcpOAuthError("invalid_scope", "Некорректный OAuth scope.")
    else:
        values = {item for item in value.split() if item}
    if not values or not values.issubset(SUPPORTED_SCOPES):
        raise FinanceMcpOAuthError("invalid_scope", "Запрошен неподдерживаемый OAuth scope.")
    if require_finance_read and FINANCE_READ_SCOPE not in values:
        raise FinanceMcpOAuthError("invalid_scope", "Для MCP необходим scope finance.read.")
    return sorted(values)


def _validate_pkce_challenge(value):
    if not isinstance(value, str) or not 43 <= len(value) <= 128:
        raise FinanceMcpOAuthError("invalid_request", "Требуется S256 PKCE code_challenge.")
    try:
        base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError) as exc:
        raise FinanceMcpOAuthError("invalid_request", "Некорректный PKCE code_challenge.") from exc
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


def _metadata_fingerprint(value):
    """Return a deterministic fingerprint without retaining metadata content."""
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def fetch_trusted_chatgpt_client_metadata():
    """Fetch and validate the one allowlisted ChatGPT public-client document.

    This helper is deliberately provisioning-only.  It has no parameter, does
    not follow redirects, bounds the response body and returns only fields
    required to pin a public client.  Runtime OAuth never fetches this URL, so
    a request cannot turn into SSRF or a network dependency.
    """
    response = None
    try:
        request = Request(
            CHATGPT_CLIENT_ID_METADATA_URL,
            headers={"Accept": "application/json"},
            method="GET",
        )
        response = build_opener(_NoRedirectHandler()).open(request, timeout=8)
        if response.getcode() != 200:
            raise FinanceMcpConfigurationError(
                "Не удалось проверить доверенный ChatGPT client metadata document."
            )
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise FinanceMcpConfigurationError(
                "Доверенный ChatGPT client metadata document должен быть JSON."
            )
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                if int(content_length) > MAX_CHATGPT_CLIENT_METADATA_BYTES:
                    raise FinanceMcpConfigurationError(
                        "Доверенный ChatGPT client metadata document слишком велик."
                    )
            except ValueError as exc:
                raise FinanceMcpConfigurationError(
                    "Некорректная длина ChatGPT client metadata document."
                ) from exc
        chunks = []
        size = 0
        while True:
            chunk = response.read(4096)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_CHATGPT_CLIENT_METADATA_BYTES:
                raise FinanceMcpConfigurationError(
                    "Доверенный ChatGPT client metadata document слишком велик."
                )
            chunks.append(chunk)
        try:
            metadata = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FinanceMcpConfigurationError(
                "Доверенный ChatGPT client metadata document содержит некорректный JSON."
            ) from exc
    except (HTTPError, URLError, OSError) as exc:
        raise FinanceMcpConfigurationError(
            "Не удалось получить доверенный ChatGPT client metadata document."
        ) from exc
    finally:
        if response is not None:
            response.close()

    if not isinstance(metadata, dict) or metadata.get("client_id") != CHATGPT_CLIENT_ID_METADATA_URL:
        raise FinanceMcpConfigurationError("ChatGPT client metadata document не подтверждает ожидаемый client_id.")
    if not isinstance(metadata.get("client_name"), str) or not metadata["client_name"].strip():
        raise FinanceMcpConfigurationError("ChatGPT client metadata document не содержит client_name.")
    redirect_uris = metadata.get("redirect_uris")
    if (
        not isinstance(redirect_uris, list)
        or not redirect_uris
        or any(not _valid_redirect_uri(uri) for uri in redirect_uris)
        or len(set(redirect_uris)) != len(redirect_uris)
    ):
        raise FinanceMcpConfigurationError("ChatGPT client metadata document содержит недопустимый redirect URI.")
    grant_types = metadata.get("grant_types")
    response_types = metadata.get("response_types")
    auth_methods = metadata.get("token_endpoint_auth_methods_supported")
    if (
        not isinstance(grant_types, list)
        or AUTHORIZATION_CODE_GRANT not in grant_types
        or not isinstance(response_types, list)
        or "code" not in response_types
        or not isinstance(auth_methods, list)
        or "none" not in auth_methods
    ):
        raise FinanceMcpConfigurationError(
            "ChatGPT client metadata document не поддерживает public-client authorization code flow."
        )
    return {
        "client_id": CHATGPT_CLIENT_ID_METADATA_URL,
        "redirect_uris": tuple(redirect_uris),
        "sha256": _metadata_fingerprint(metadata),
    }


def _is_pinned_chatgpt_cimd_client(client):
    """Validate stored evidence without any request-time metadata fetch."""
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
        raise FinanceMcpOAuthError("invalid_client", "Неизвестный OAuth клиент.", status=401)
    client = FinanceMcpClient.objects.select_related("principal").filter(
        client_id=client_id,
        is_active=True,
        revoked_at__isnull=True,
        principal__is_active=True,
        principal__revoked_at__isnull=True,
    ).first()
    if not _is_pinned_chatgpt_cimd_client(client):
        raise FinanceMcpOAuthError("invalid_client", "Неизвестный OAuth клиент.", status=401)
    # Exact equality is intentional; prefix/host aliases and wildcard redirect
    # URI matching would weaken authorization-code delivery.
    if not _valid_redirect_uri(redirect_uri) or redirect_uri not in client.redirect_uris:
        raise FinanceMcpOAuthError("invalid_request", "Redirect URI не зарегистрирован.")
    return client


def validate_authorization_request(params):
    if not isinstance(params, dict):
        raise FinanceMcpOAuthError("invalid_request", "Некорректный OAuth запрос.")
    if params.get("response_type") != "code":
        raise FinanceMcpOAuthError("unsupported_response_type", "Поддерживается только authorization code.")
    state = params.get("state")
    if not isinstance(state, str) or not state or len(state) > 2048:
        raise FinanceMcpOAuthError("invalid_request", "OAuth state обязателен.")
    if params.get("code_challenge_method") != "S256":
        raise FinanceMcpOAuthError("invalid_request", "Требуется PKCE S256.")
    challenge = _validate_pkce_challenge(params.get("code_challenge"))
    resource = params.get("resource")
    if resource != resource_url():
        raise FinanceMcpOAuthError("invalid_target", "OAuth resource не соответствует финансовому MCP.")
    client = client_for_authorization(params.get("client_id"), params.get("redirect_uri"))
    scopes = _normalize_scopes(params.get("scope"))
    return {
        "client": client,
        "state": state,
        "redirect_uri": params["redirect_uri"],
        "resource": resource,
        "scopes": scopes,
        "code_challenge": challenge,
    }


def authorization_is_allowed(user, client):
    if not user or not user.is_authenticated:
        return False
    organizations = list(
        client.principal.organization_scopes.select_related("organization").all()
    )
    return bool(organizations) and all(
        can_manage_finance(user, item.organization) for item in organizations
    )


def scoped_organizations(client):
    return list(
        client.principal.organization_scopes.select_related("organization")
        .order_by("organization__name", "organization_id")
    )


def _revoke_grants(queryset, *, now, reason):
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


def revoke_grant(grant, *, reason):
    """Owner/administrator path used by the explicit management command."""
    now = timezone.now()
    with transaction.atomic():
        _revoke_grants(FinanceMcpGrant.objects.filter(pk=grant.pk, revoked_at__isnull=True), now=now, reason=reason)


def revoke_client(client, *, reason):
    """Revoke every active grant/token for a pre-registered client."""
    now = timezone.now()
    with transaction.atomic():
        FinanceMcpClient.objects.filter(pk=client.pk, revoked_at__isnull=True).update(
            is_active=False, revoked_at=now
        )
        _revoke_grants(
            FinanceMcpGrant.objects.filter(client_id=client.pk, revoked_at__isnull=True),
            now=now,
            reason=reason,
        )


def issue_authorization_code(*, authorization, user):
    client = authorization["client"]
    if not authorization_is_allowed(user, client):
        raise FinanceMcpOAuthError("access_denied", "Недостаточно прав для выдачи финансового доступа.", status=403)
    now = timezone.now()
    raw_code = _random_secret()
    with transaction.atomic():
        # Fresh consent invalidates every earlier active grant for this exact
        # client/principal.  This prevents an old refresh-token family from
        # silently surviving a deliberate re-authorization.
        _revoke_grants(
            FinanceMcpGrant.objects.filter(
                client=client,
                principal=client.principal,
                revoked_at__isnull=True,
            ),
            now=now,
            reason="re-authorized",
        )
        grant = FinanceMcpGrant.objects.create(
            client=client,
            principal=client.principal,
            authorized_by=user,
            scopes=authorization["scopes"],
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
    return bool(
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
        refresh_values = {
            "grant": grant,
            "token_hash": _secret_hash(raw_refresh),
            "scopes": scopes,
            "expires_at": now + timedelta(seconds=refresh_token_ttl_seconds()),
        }
        if family_id is not None:
            refresh_values["family_id"] = family_id
        refresh = FinanceMcpRefreshToken.objects.create(
            **refresh_values,
        )
    response = {
        "access_token": raw_access,
        "token_type": "Bearer",
        "expires_in": access_token_ttl_seconds(),
        "scope": " ".join(scopes),
    }
    if raw_refresh is not None:
        response["refresh_token"] = raw_refresh
    return response, access, refresh


def exchange_authorization_code(params):
    required = ("code", "client_id", "redirect_uri", "code_verifier", "resource")
    if any(not params.get(key) for key in required):
        raise FinanceMcpOAuthError("invalid_request", "Не передан обязательный OAuth параметр.")
    if params.get("resource") != resource_url():
        raise FinanceMcpOAuthError("invalid_target", "OAuth resource не соответствует финансовому MCP.")
    client = client_for_authorization(params.get("client_id"), params.get("redirect_uri"))
    now = timezone.now()
    with transaction.atomic():
        code = FinanceMcpAuthorizationCode.objects.select_for_update().select_related(
            "grant__client__principal"
        ).filter(code_hash=_secret_hash(params.get("code"))).first()
        if code is None or code.used_at is not None or code.expires_at <= now:
            raise FinanceMcpOAuthError("invalid_grant", "Authorization code недействителен.")
        if (
            code.redirect_uri != params.get("redirect_uri")
            or code.resource != params.get("resource")
            or not _verify_pkce(params.get("code_verifier"), code.code_challenge)
            or not _grant_is_valid(code.grant, now=now, client=client, resource=code.resource)
        ):
            raise FinanceMcpOAuthError("invalid_grant", "Authorization code недействителен.")
        code.used_at = now
        code.save(update_fields=["used_at"])
        response, _, _ = _issue_tokens(code.grant, scopes=list(code.scopes))
    return response


def exchange_refresh_token(params):
    required = ("refresh_token", "client_id", "resource")
    if any(not params.get(key) for key in required):
        raise FinanceMcpOAuthError("invalid_request", "Не передан обязательный OAuth параметр.")
    if params.get("resource") != resource_url():
        raise FinanceMcpOAuthError("invalid_target", "OAuth resource не соответствует финансовому MCP.")
    # Refresh requests do not carry a redirect URI, so resolve client by ID
    # here and retain the exact resource binding from the original grant.
    client = FinanceMcpClient.objects.select_related("principal").filter(
        client_id=params.get("client_id"),
        is_active=True,
        revoked_at__isnull=True,
        principal__is_active=True,
        principal__revoked_at__isnull=True,
    ).first()
    if not _is_pinned_chatgpt_cimd_client(client):
        raise FinanceMcpOAuthError("invalid_client", "Неизвестный OAuth клиент.", status=401)
    requested_scopes = _normalize_scopes(params.get("scope")) if params.get("scope") else None
    now = timezone.now()
    reuse_or_invalid = False
    response = None
    with transaction.atomic():
        refresh = FinanceMcpRefreshToken.objects.select_for_update().select_related(
            "grant__client__principal"
        ).filter(token_hash=_secret_hash(params.get("refresh_token"))).first()
        if refresh is None:
            raise FinanceMcpOAuthError("invalid_grant", "Refresh token недействителен.")
        valid = (
            refresh.used_at is None
            and refresh.revoked_at is None
            and refresh.expires_at > now
            and _grant_is_valid(refresh.grant, now=now, client=client, resource=params.get("resource"))
        )
        if not valid:
            # A replayed/expired/explicitly revoked refresh token must never
            # issue a replacement.  Reuse of an otherwise live family revokes
            # that entire grant and all access tokens immediately.
            _revoke_grants(
                FinanceMcpGrant.objects.filter(pk=refresh.grant_id, revoked_at__isnull=True),
                now=now,
                reason="refresh-token reuse or invalid refresh",
            )
            # Do not raise while inside this ``atomic`` block: an exception
            # would roll the defensive revocation back.  Return the OAuth
            # error only after the transaction commits.
            reuse_or_invalid = True
        else:
            original_scopes = set(refresh.scopes)
            scopes = sorted(original_scopes if requested_scopes is None else set(requested_scopes))
            if not set(scopes).issubset(original_scopes):
                raise FinanceMcpOAuthError("invalid_scope", "Нельзя расширить scope через refresh token.")
            if FINANCE_READ_SCOPE not in scopes:
                raise FinanceMcpOAuthError("invalid_scope", "Для MCP необходим scope finance.read.")
            refresh.used_at = now
            response, _, replacement = _issue_tokens(
                refresh.grant,
                scopes=scopes,
                family_id=refresh.family_id,
            )
            refresh.replaced_by = replacement
            refresh.save(update_fields=["used_at", "replaced_by"])
    if reuse_or_invalid:
        raise FinanceMcpOAuthError("invalid_grant", "Refresh token недействителен.")
    return response


def exchange_token(params):
    if not isinstance(params, dict):
        raise FinanceMcpOAuthError("invalid_request", "Некорректный OAuth запрос.")
    grant_type = params.get("grant_type")
    if grant_type == AUTHORIZATION_CODE_GRANT:
        return exchange_authorization_code(params)
    if grant_type == REFRESH_TOKEN_GRANT:
        return exchange_refresh_token(params)
    raise FinanceMcpOAuthError("unsupported_grant_type", "Поддерживаются authorization_code и refresh_token.")


def authenticate_bearer_header(value) -> AuthenticatedFinanceMcpRequest:
    if not isinstance(value, str) or not value.startswith("Bearer "):
        raise FinanceMcpOAuthError("invalid_token", "Требуется Bearer access token.", status=401)
    raw_token = value.removeprefix("Bearer ")
    if not raw_token or any(character.isspace() for character in raw_token):
        raise FinanceMcpOAuthError("invalid_token", "Требуется Bearer access token.", status=401)
    now = timezone.now()
    token = FinanceMcpAccessToken.objects.select_related(
        "grant__principal", "grant__client"
    ).filter(token_hash=_secret_hash(raw_token)).first()
    if token is None:
        raise FinanceMcpOAuthError("invalid_token", "Access token недействителен.", status=401)
    grant = token.grant
    client = grant.client
    if not (
        token.expires_at > now
        and token.revoked_at is None
        and token.audience == resource_url()
        and FINANCE_READ_SCOPE in token.scopes
        and _grant_is_valid(grant, now=now, client=client, resource=token.audience)
    ):
        raise FinanceMcpOAuthError("invalid_token", "Access token недействителен.", status=401)
    # This is audit-adjacent metadata, never a financial table write.  It is
    # intentionally best-effort and does not extend the token's lifetime.
    FinanceMcpAccessToken.objects.filter(pk=token.pk).update(last_used_at=now)
    return AuthenticatedFinanceMcpRequest(token=token, grant=grant, principal=grant.principal)


def authorization_redirect_uri(redirect_uri, *, code=None, state=None, error=None, error_description=None):
    """Build a redirect only after redirect_uri passed exact client allowlisting."""
    parts = urlsplit(redirect_uri)
    existing = parts.query
    query = {}
    if existing:
        # Registered redirect URI may include a static query.  Preserve it,
        # but never use it to select a client or resource.
        from urllib.parse import parse_qsl

        query.update(parse_qsl(existing, keep_blank_values=True))
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
        # offline_access is intentionally not a resource scope; the client may
        # request it from authorization-server metadata when it wants refresh.
        "scopes_supported": [FINANCE_READ_SCOPE],
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
        "scopes_supported": [FINANCE_READ_SCOPE, OFFLINE_ACCESS_SCOPE],
        "token_endpoint_auth_methods_supported": ["none"],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
    }


__all__ = [
    "AUTHORIZATION_CODE_GRANT",
    "CHATGPT_CLIENT_ID_METADATA_URL",
    "FINANCE_READ_SCOPE",
    "FinanceMcpConfigurationError",
    "FinanceMcpOAuthError",
    "OFFLINE_ACCESS_SCOPE",
    "authorization_endpoint_url",
    "authorization_redirect_uri",
    "authorization_server_metadata",
    "authenticate_bearer_header",
    "client_for_authorization",
    "exchange_token",
    "finance_mcp_allowed_origins",
    "finance_mcp_origin_is_allowed",
    "fetch_trusted_chatgpt_client_metadata",
    "is_enabled",
    "issue_authorization_code",
    "protected_resource_metadata",
    "protected_resource_metadata_url",
    "resource_url",
    "revoke_client",
    "revoke_grant",
    "scoped_organizations",
    "token_endpoint_url",
    "validate_authorization_request",
]
