"""Dedicated read-only MCP resource over the existing Avito saved workspace."""
from __future__ import annotations
import json
from time import monotonic
from django.core import signing
from django.core.exceptions import PermissionDenied
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseNotFound, HttpResponseServerError, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from pool_service import onec_diagnostic_mcp_views as transport
from pool_service.models import FinanceMcpAuditEvent
from pool_service.avito_mcp_auth import (
    AVITO_READ_SCOPE, AvitoMcpConfigurationError, AvitoMcpOAuthError,
    authenticate_bearer_header, authorization_redirect_uri, exchange_token,
    is_enabled, issue_authorization_code, avito_mcp_origin_is_allowed,
    protected_resource_metadata, protected_resource_metadata_url,
    revalidate_authenticated_request, resource_url, scoped_organizations,
    target_organization, validate_authorization_request,
)
from pool_service.avito_mcp_reader import TOOL_NAMES, read_tool


def _tool_definitions():
    descriptions = {
        "get_avito_funds": "Read separately saved Avito CPA advance, wallet cash and bonuses; absent or unbound values remain unknown.",
        "get_avito_listings": "Read saved listing statuses and known deadlines with their provider source and observation times; never reactivate.",
        "get_avito_upload_report": "Read the saved detailed upload result and separate XML publication/source/content proofs; never publish.",
        "get_avito_statistics": "Read retained source-date period reports and daily-history coverage; all pages, ad coverage and known metrics are separate.",
        "get_avito_diagnostics": "Read connection-scoped background checks; no tokens, client messages, settings dump or provider refresh.",
    }
    result = []
    for name in TOOL_NAMES:
        properties = {"connection_id": {"type": "integer", "enum": [4]}}
        if name == "get_avito_statistics":
            properties.update(date_from={"type": "string", "format": "date"},
                              date_to={"type": "string", "format": "date"})
        schemes = [{"type": "oauth2", "scopes": [AVITO_READ_SCOPE]}]
        result.append({
            "name": name, "description": descriptions[name],
            "inputSchema": {"type": "object", "properties": properties,
                            "required": ["connection_id"], "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False},
            "securitySchemes": schemes, "_meta": {"securitySchemes": schemes},
        })
    return result


def _tool_dispatch(authenticated, name, arguments):
    refreshed = revalidate_authenticated_request(authenticated)
    result = read_tool(refreshed.grant.authorized_by, name, arguments)
    # Reject token, identity or account revocation before releasing the snapshot.
    revalidate_authenticated_request(refreshed)
    if len(json.dumps(result, ensure_ascii=False).encode()) > 262144:
        raise ValueError("response_limit")
    return result


def _audit(authenticated, name, *, result, started, response_bytes=0):
    # Audit identifiers, outcome and size only; no arguments/results or account secrets.
    scopes = authenticated.grant.scopes if isinstance(authenticated.grant.scopes, list) else []
    organizations = [int(item.rsplit(":", 1)[1]) for item in scopes
        if isinstance(item, str) and item.startswith("service2.avito.organization:")
        and item.rsplit(":", 1)[1].isdigit()]
    FinanceMcpAuditEvent.objects.create(
        principal=authenticated.principal,
        grant=authenticated.grant,
        tool_name="avito." + (name if name in TOOL_NAMES else "invalid_tool"),
        organization_ids=organizations[:1],
        result=result,
        duration_ms=max(0, int((monotonic() - started) * 1000)),
        response_bytes=max(0, int(response_bytes)),
    )


def _challenge(*, error=None, description=None):
    values = [
        f'resource_metadata="{protected_resource_metadata_url()}"',
        f'scope="{AVITO_READ_SCOPE}"',
        f'error="{error}"' if error else None,
        f'error_description="{description}"' if description else None,
    ]
    return "Bearer " + ", ".join(value for value in values if value)


def _tool_auth_required(request_id, *, protocol_version):
    return transport._mcp_response(
        transport._jsonrpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": "Authentication required before reading Avito snapshots."}],
                "_meta": {"mcp/www_authenticate": [_challenge(error="insufficient_scope", description="Link Service2 Avito to continue")]},
                "isError": True,
            },
        ),
        protocol_version=protocol_version,
    )


def _http_auth_required(*, protocol_version):
    response = transport._mcp_response(
        transport._jsonrpc_error(None, -32001, "Avito MCP requires a valid Bearer token."),
        status=401,
        protocol_version=protocol_version,
    )
    response["WWW-Authenticate"] = _challenge()
    return response


@csrf_exempt
def avito_mcp(request):
    protocol_version = transport._protocol_version(request)
    if not is_enabled():
        return transport._no_store(HttpResponseNotFound())
    try:
        protected_resource_metadata()
        origin_allowed = avito_mcp_origin_is_allowed(request.headers.get("Origin"))
    except AvitoMcpConfigurationError:
        return transport._mcp_empty(status=503, protocol_version=protocol_version)
    if not origin_allowed:
        return transport._mcp_empty(status=403, protocol_version=protocol_version)

    if request.method == "OPTIONS":
        response = transport._mcp_empty(status=204, protocol_version=protocol_version)
        response["Allow"] = "POST, OPTIONS"
        return response
    if request.method != "POST":
        response = transport._mcp_empty(status=405, protocol_version=protocol_version)
        response["Allow"] = "POST, OPTIONS"
        return response

    authorization = request.headers.get("Authorization")
    authenticated = None
    if authorization:
        try:
            authenticated = authenticate_bearer_header(authorization)
        except AvitoMcpOAuthError as exc:
            response = transport._mcp_response(
                transport._jsonrpc_error(None, -32001, "Avito MCP authentication failed."),
                status=exc.status,
                protocol_version=protocol_version,
            )
            response["WWW-Authenticate"] = _challenge(error=exc.error, description=exc.description)
            return response

    payload, error_response = transport._require_json_mcp_request(request, protocol_version=protocol_version)
    if error_response is not None:
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return error_response

    request_id = payload.get("id")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return transport._mcp_response(
            transport._jsonrpc_error(request_id, -32602, "Invalid params"),
            protocol_version=protocol_version,
        )

    method = payload["method"]
    if method == "notifications/initialized":
        if "id" in payload:
            return transport._mcp_response(
                transport._jsonrpc_error(request_id, -32600, "notifications/initialized must not include an id"),
                status=400,
                protocol_version=protocol_version,
            )
        return transport._mcp_empty(status=202, protocol_version=protocol_version)

    if "id" not in payload:
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return transport._mcp_empty(status=202, protocol_version=protocol_version)

    if method == "initialize":
        requested_version = params.get("protocolVersion")
        client_info = params.get("clientInfo")
        if (
            not isinstance(requested_version, str)
            or not isinstance(params.get("capabilities"), dict)
            or not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not client_info.get("name")
            or not isinstance(client_info.get("version"), str)
            or not client_info.get("version")
        ):
            if not authorization:
                return _http_auth_required(protocol_version=protocol_version)
            return transport._mcp_response(
                transport._jsonrpc_error(request_id, -32602, "Invalid initialize params"),
                protocol_version=protocol_version,
            )
        selected = requested_version if requested_version in transport.SUPPORTED_PROTOCOL_VERSIONS else transport.MCP_PROTOCOL_VERSION
        return transport._mcp_response(
            transport._jsonrpc_result(
                request_id,
                {
                    "protocolVersion": selected,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "service2-avito", "version": "1.0.0"},
                    "instructions": "Read saved Services connection 4 snapshots only. A saved result is not a new provider request. No ads, messages, payments or settings can be changed.",
                },
            ),
            protocol_version=selected,
        )

    if request.headers.get("MCP-Protocol-Version") not in transport.SUPPORTED_PROTOCOL_VERSIONS:
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return transport._mcp_response(
            transport._jsonrpc_error(request_id, -32600, "Unsupported MCP-Protocol-Version"),
            status=400,
            protocol_version=protocol_version,
        )

    if method == "tools/list":
        return transport._mcp_response(
            transport._jsonrpc_result(
                request_id, {"tools": _tool_definitions()}
            ),
            protocol_version=protocol_version,
        )

    if method != "tools/call":
        return transport._mcp_response(
            transport._jsonrpc_error(request_id, -32601, "Method not found"),
            protocol_version=protocol_version,
        )
    if authenticated is None:
        return _tool_auth_required(request_id, protocol_version=protocol_version)

    name = params.get("name")
    arguments = params.get("arguments", {})
    started = monotonic()
    if not isinstance(name, str) or name not in TOOL_NAMES or not isinstance(arguments, dict):
        _audit(authenticated, name, result="denied", started=started)
        return transport._mcp_response(
            transport._jsonrpc_error(request_id, -32602, "Unknown tool or invalid arguments"),
            protocol_version=protocol_version,
        )

    try:
        with transaction.atomic():
            data = _tool_dispatch(authenticated, name, arguments)
            response_bytes = len(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            _audit(
                authenticated,
                name,
                result="success",
                started=started,
                response_bytes=response_bytes,
            )
    except (ValueError, PermissionError, PermissionDenied):
        _audit(authenticated, name, result="denied", started=started)
        return transport._mcp_response(
            transport._jsonrpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": "Avito MCP rejected the request by server policy."}],
                    "isError": True,
                },
            ),
            protocol_version=protocol_version,
        )
    except Exception:
        _audit(authenticated, name, result="error", started=started)
        return transport._mcp_response(
            transport._jsonrpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": "Service2 could not safely read the saved snapshot."}],
                    "isError": True,
                },
            ),
            protocol_version=protocol_version,
        )

    return transport._mcp_response(
        transport._jsonrpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": "Saved Avito snapshot read."}],
                "structuredContent": data,
                "isError": False,
            },
        ),
        protocol_version=protocol_version,
    )


def avito_protected_resource_metadata(request):
    if request.method != "GET":
        response = HttpResponse(status=405)
        response["Allow"] = "GET"
        return transport._no_store(response)
    if not is_enabled():
        return transport._no_store(HttpResponseNotFound())
    try:
        return transport._no_store(JsonResponse(protected_resource_metadata()))
    except AvitoMcpConfigurationError:
        return transport._no_store(HttpResponseServerError())


def _single_value_params(query_dict):
    if any(len(values) != 1 for _key, values in query_dict.lists()):
        raise AvitoMcpOAuthError("invalid_request", "OAuth parameters must not repeat.")
    return {key: values[0] for key, values in query_dict.lists()}


def _oauth_error_page(_error):
    return transport._no_store(HttpResponseBadRequest("OAuth authorization request was rejected."))


_CONSENT_BINDING_SALT = "service2.avito-mcp.consent.v1"
_CONSENT_BINDING_MAX_AGE = 600


def _consent_binding(authorization):
    return signing.dumps(
        {
            "organization_id": authorization["organization_id"],
            "state": authorization["state"],
            "client_id": authorization["client"].client_id,
            "resource": authorization["resource"],
            "redirect_uri": authorization["redirect_uri"],
            "code_challenge": authorization["code_challenge"],
            "scopes": sorted(authorization["scopes"]),
            "binding_scope": authorization["binding_scope"],
        },
        salt=_CONSENT_BINDING_SALT,
        compress=True,
    )


def _validate_consent_binding(value, authorization):
    if not isinstance(value, str) or not value:
        return False
    try:
        bound = signing.loads(
            value,
            salt=_CONSENT_BINDING_SALT,
            max_age=_CONSENT_BINDING_MAX_AGE,
        )
    except signing.BadSignature:
        return False
    expected = {
        "organization_id": authorization["organization_id"],
        "state": authorization["state"],
        "client_id": authorization["client"].client_id,
        "resource": authorization["resource"],
        "redirect_uri": authorization["redirect_uri"],
        "code_challenge": authorization["code_challenge"],
        "scopes": sorted(authorization["scopes"]),
        "binding_scope": authorization["binding_scope"],
    }
    return bound == expected


@require_http_methods(["GET", "POST"])
def avito_oauth_authorize(request):
    if not is_enabled():
        return transport._no_store(HttpResponseNotFound())
    if not request.user.is_authenticated:
        return transport._no_store(redirect_to_login(request.get_full_path()))
    try:
        params = _single_value_params(request.GET if request.method == "GET" else request.POST)
        authorization = validate_authorization_request(params)
    except (AvitoMcpOAuthError, AvitoMcpConfigurationError) as exc:
        return _oauth_error_page(exc)

    client = authorization["client"]
    from pool_service.avito_mcp_auth import authorization_is_allowed

    if not authorization_is_allowed(request.user, client):
        return transport._no_store(
            redirect(
                authorization_redirect_uri(
                    authorization["redirect_uri"],
                    state=authorization["state"],
                    error="access_denied",
                )
            )
        )

    if request.method == "GET":
        return transport._no_store(
            render(
                request,
                "pool_service/avito_mcp/authorize.html",
                {
                    "client": client,
                    "organizations": scoped_organizations(client),
                    "scope": " ".join(authorization["scopes"]),
                    "oauth_params": params,
                    "consent_binding": _consent_binding(authorization),
                },
            )
        )

    if request.method == "POST" and not _validate_consent_binding(
        request.POST.get("consent_binding"),
        authorization,
    ):
        return transport._no_store(
            redirect(
                authorization_redirect_uri(
                    authorization["redirect_uri"],
                    state=authorization["state"],
                    error="access_denied",
                )
            )
        )

    if request.POST.get("decision") != "approve":
        return transport._no_store(
            redirect(
                authorization_redirect_uri(
                    authorization["redirect_uri"],
                    state=authorization["state"],
                    error="access_denied",
                )
            )
        )

    try:
        code = issue_authorization_code(authorization=authorization, user=request.user)
    except AvitoMcpOAuthError:
        return transport._no_store(
            redirect(
                authorization_redirect_uri(
                    authorization["redirect_uri"],
                    state=authorization["state"],
                    error="access_denied",
                )
            )
        )
    return transport._no_store(
        redirect(
            authorization_redirect_uri(
                authorization["redirect_uri"],
                code=code,
                state=authorization["state"],
            )
        )
    )


@csrf_exempt
@require_http_methods(["POST"])
def avito_oauth_token(request):
    if not is_enabled():
        return transport._no_store(HttpResponseNotFound())
    if request.content_type != "application/x-www-form-urlencoded":
        return transport._no_store(
            JsonResponse(
                {
                    "error": "invalid_request",
                    "error_description": "Content-Type must be application/x-www-form-urlencoded",
                },
                status=415,
            )
        )
    try:
        result = exchange_token(_single_value_params(request.POST))
    except AvitoMcpOAuthError as exc:
        response = JsonResponse(
            {"error": exc.error, "error_description": exc.description},
            status=exc.status,
        )
        if exc.status == 401:
            response["WWW-Authenticate"] = 'Basic realm="service2-avito"'
        return transport._no_store(response)
    except AvitoMcpConfigurationError:
        return transport._no_store(HttpResponseServerError())
    return transport._no_store(JsonResponse(result))


__all__ = [
    "TOOL_NAMES",
    "avito_mcp",
    "avito_oauth_authorize",
    "avito_oauth_token",
    "avito_protected_resource_metadata",
]
