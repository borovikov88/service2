"""ChatGPT-facing adapter for the read-only 1C Diagnostic MCP.

ChatGPT must be able to initialize the MCP connection and enumerate tool schemas
before the end user has linked OAuth.  Tool execution remains protected.  This
adapter also keeps all public OAuth discovery URLs on the canonical Diagnostic
MCP host instead of inheriting the unrelated application SITE_URL.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from pool_service import onec_diagnostic_mcp_views as legacy
from pool_service.onec_diagnostic_mcp_auth import (
    DIAGNOSTIC_READ_SCOPE,
    authorization_server_metadata,
    is_enabled,
    protected_resource_metadata,
    resource_url,
    issuer_url,
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


def _authorization_server_metadata_url():
    parsed = urlsplit(issuer_url())
    issuer_path = parsed.path.rstrip("/")
    return urlunsplit((
        parsed.scheme,
        parsed.netloc,
        f"/.well-known/oauth-authorization-server{issuer_path}",
        "",
        "",
    ))


def _challenge(*, error=None, description=None):
    values = [
        f'resource_metadata="{_protected_resource_metadata_url()}"',
        f'scope="{DIAGNOSTIC_READ_SCOPE}"',
        f'error="{error}"' if error else None,
        f'error_description="{description}"' if description else None,
    ]
    return "Bearer " + ", ".join(value for value in values if value)


def _tool_definitions():
    tools = legacy._tool_definitions()
    for tool in tools:
        tool["securitySchemes"] = [
            {"type": "oauth2", "scopes": [DIAGNOSTIC_READ_SCOPE]}
        ]
    return tools


def _auth_required_result(request_id):
    return legacy._mcp_response(
        legacy._jsonrpc_result(
            request_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": "Authentication required before this read-only 1C diagnostic tool can run.",
                    }
                ],
                "_meta": {
                    "mcp/www_authenticate": [
                        _challenge(
                            error="insufficient_scope",
                            description="Link Service2 to continue",
                        )
                    ]
                },
                "isError": True,
            },
        )
    )


@csrf_exempt
def onec_diagnostic_mcp(request):
    """Expose discovery anonymously; enforce OAuth for every tool execution."""
    protocol_version = legacy._protocol_version(request)
    if not is_enabled():
        return legacy._no_store(legacy.HttpResponseNotFound())

    if request.method == "OPTIONS":
        return legacy.onec_diagnostic_mcp(request)
    if request.method != "POST":
        return legacy.onec_diagnostic_mcp(request)

    payload, error_response = legacy._require_json_mcp_request(
        request, protocol_version=protocol_version
    )
    if error_response is not None:
        return error_response

    request_id = payload.get("id")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        return legacy._mcp_response(
            legacy._jsonrpc_error(request_id, -32602, "Invalid params"),
            protocol_version=protocol_version,
        )

    method = payload["method"]
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
                        "version": "1.0.1",
                    },
                    "instructions": "Controlled read-only live 1C diagnostics. OAuth is required to execute tools.",
                },
            ),
            protocol_version=selected,
        )

    if method == "notifications/initialized":
        if "id" in payload:
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

    if request.headers.get("MCP-Protocol-Version") not in legacy.SUPPORTED_PROTOCOL_VERSIONS:
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

    if method == "tools/call" and not request.headers.get("Authorization"):
        return _auth_required_result(request_id)

    response = legacy.onec_diagnostic_mcp(request)
    if response.status_code == 401:
        response["WWW-Authenticate"] = _challenge(error="invalid_token")
    return response


def onec_diagnostic_protected_resource_metadata(request):
    if request.method != "GET":
        return legacy.onec_diagnostic_protected_resource_metadata(request)
    data = protected_resource_metadata()
    return legacy._no_store(JsonResponse(data))


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
