"""ChatGPT-facing adapter for the read-only 1C Diagnostic MCP.

ChatGPT needs the static tool descriptors before a user can link OAuth.  The
adapter therefore exposes only MCP initialize/tool discovery anonymously and
keeps every 1C data operation behind the reviewed Bearer-token implementation.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from pool_service import onec_diagnostic_mcp_views as legacy
from pool_service.onec_diagnostic_mcp_auth import (
    DIAGNOSTIC_READ_SCOPE,
    authorization_server_metadata,
    issuer_url,
    is_enabled,
    protected_resource_metadata,
    resource_url,
)


def _origin(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _protected_resource_metadata_url():
    parsed = urlsplit(resource_url())
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        "/.well-known/oauth-protected-resource/mcp/1c",
        "",
        "",
    ))


def _oauth_security_schemes():
    return [{"type": "oauth2", "scopes": [DIAGNOSTIC_READ_SCOPE]}]


def _tool_definitions():
    """Return only static schemas, explicitly marking every tool as OAuth-only."""
    tools = legacy._tool_definitions()
    for tool in tools:
        schemes = _oauth_security_schemes()
        tool["securitySchemes"] = schemes
        # Keep the compatibility mirror used by older OpenAI/MCP clients.  It
        # contains policy metadata only and never exposes a token or 1C data.
        meta = tool.get("_meta")
        meta = dict(meta) if isinstance(meta, dict) else {}
        meta["securitySchemes"] = _oauth_security_schemes()
        tool["_meta"] = meta
    return tools


def _tool_auth_challenge(*, error="insufficient_scope", description="Link Service2 to continue"):
    values = [
        f'resource_metadata="{_protected_resource_metadata_url()}"',
        f'scope="{DIAGNOSTIC_READ_SCOPE}"',
        f'error="{error}"' if error else None,
        f'error_description="{description}"' if description else None,
    ]
    return "Bearer " + ", ".join(value for value in values if value)


def _tool_auth_required(request_id, *, protocol_version):
    return legacy._mcp_response(
        legacy._jsonrpc_result(
            request_id,
            {
                "content": [{
                    "type": "text",
                    "text": "Authentication required before this read-only 1C diagnostic tool can run.",
                }],
                "_meta": {
                    "mcp/www_authenticate": [_tool_auth_challenge()],
                },
                "isError": True,
            },
        ),
        protocol_version=protocol_version,
    )


def _http_auth_required(*, protocol_version):
    """Preserve the original fail-closed response for non-discovery traffic."""
    response = legacy._mcp_response(
        legacy._jsonrpc_error(
            None,
            -32001,
            "Diagnostic MCP требует действующий read-only Bearer token.",
        ),
        status=401,
        protocol_version=protocol_version,
    )
    response["WWW-Authenticate"] = _tool_auth_challenge(
        error=None,
        description=None,
    )
    return response


# The reviewed transport's Bearer challenge resolves this name from its module
# globals. Bind it to the canonical Diagnostic resource host rather than the
# unrelated application SITE_URL.
legacy.protected_resource_metadata_url = _protected_resource_metadata_url


@csrf_exempt
def onec_diagnostic_mcp(request):
    """Expose static discovery anonymously; require OAuth for all tool calls."""
    protocol_version = legacy._protocol_version(request)
    if not is_enabled():
        return legacy._no_store(legacy.HttpResponseNotFound())

    try:
        protected_resource_metadata()
        origin_allowed = legacy.diagnostic_mcp_origin_is_allowed(
            request.headers.get("Origin")
        )
    except legacy.OneCDiagnosticMcpConfigurationError:
        return legacy._mcp_empty(status=503, protocol_version=protocol_version)
    if not origin_allowed:
        return legacy._mcp_empty(status=403, protocol_version=protocol_version)

    if request.method == "OPTIONS":
        response = legacy._mcp_empty(status=204, protocol_version=protocol_version)
        response["Allow"] = "POST, OPTIONS"
        return response
    if request.method != "POST":
        response = legacy._mcp_empty(status=405, protocol_version=protocol_version)
        response["Allow"] = "POST, OPTIONS"
        return response

    # A supplied credential is never ignored, including during discovery.  This
    # preserves immediate revocation and resource isolation for linked clients.
    authorization = request.headers.get("Authorization")
    if authorization:
        try:
            legacy.authenticate_bearer_header(authorization)
        except legacy.OneCDiagnosticMcpOAuthError as exc:
            return legacy._authorization_error_response(
                exc, protocol_version=protocol_version
            )

    payload, error_response = legacy._require_json_mcp_request(
        request, protocol_version=protocol_version
    )
    if error_response is not None:
        # Anonymous access exists only to enumerate the fixed tool schemas.  Do
        # not turn malformed unauthenticated traffic into a JSON/parser oracle.
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return error_response

    request_id = payload.get("id")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return legacy._mcp_response(
            legacy._jsonrpc_error(request_id, -32602, "Invalid params"),
            protocol_version=protocol_version,
        )

    method = payload["method"]
    if method == "notifications/initialized":
        if "id" in payload:
            if not authorization:
                return _http_auth_required(protocol_version=protocol_version)
            return legacy._mcp_response(
                legacy._jsonrpc_error(
                    request_id,
                    -32600,
                    "notifications/initialized must not include an id",
                ),
                status=400,
                protocol_version=protocol_version,
            )
        return legacy._mcp_empty(status=202, protocol_version=protocol_version)

    if "id" not in payload:
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return legacy._mcp_empty(status=202, protocol_version=protocol_version)

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
            return legacy._mcp_response(
                legacy._jsonrpc_error(request_id, -32602, "Invalid initialize params"),
                protocol_version=protocol_version,
            )
        selected = (
            requested_version
            if requested_version in legacy.SUPPORTED_PROTOCOL_VERSIONS
            else legacy.MCP_PROTOCOL_VERSION
        )
        return legacy._mcp_response(
            legacy._jsonrpc_result(
                request_id,
                {
                    "protocolVersion": selected,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "service2-onec-diagnostic-readonly",
                        "version": "1.0.2",
                    },
                    "instructions": (
                        "Controlled read-only live 1C diagnostics. OAuth is required "
                        "before any tool can read 1C data."
                    ),
                },
            ),
            protocol_version=selected,
        )

    if request.headers.get("MCP-Protocol-Version") not in legacy.SUPPORTED_PROTOCOL_VERSIONS:
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return legacy._mcp_response(
            legacy._jsonrpc_error(request_id, -32600, "Unsupported MCP-Protocol-Version"),
            status=400,
            protocol_version=protocol_version,
        )

    if method == "tools/list":
        return legacy._mcp_response(
            legacy._jsonrpc_result(request_id, {"tools": _tool_definitions()}),
            protocol_version=protocol_version,
        )

    if method == "tools/call" and not authorization:
        return _tool_auth_required(request_id, protocol_version=protocol_version)

    if authorization:
        # The reviewed endpoint re-validates the Bearer token, validates tool
        # arguments, enforces the 1C policy gateway and writes the audit record.
        return legacy.onec_diagnostic_mcp(request)

    return _http_auth_required(protocol_version=protocol_version)


def onec_diagnostic_protected_resource_metadata(request):
    if request.method != "GET":
        return legacy.onec_diagnostic_protected_resource_metadata(request)
    return legacy._no_store(JsonResponse(protected_resource_metadata()))


def onec_diagnostic_authorization_server_metadata(request):
    if request.method != "GET":
        return legacy.onec_diagnostic_authorization_server_metadata(request)
    data = authorization_server_metadata()
    base = _origin(issuer_url())
    data["authorization_endpoint"] = f"{base}/oauth/1c/authorize"
    data["token_endpoint"] = f"{base}/oauth/1c/token"
    return legacy._no_store(JsonResponse(data))


# OAuth execution itself remains in the reviewed implementation.
onec_diagnostic_oauth_authorize = legacy.onec_diagnostic_oauth_authorize
onec_diagnostic_oauth_token = legacy.onec_diagnostic_oauth_token
