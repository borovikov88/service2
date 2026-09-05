import json

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt


MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", MCP_PROTOCOL_VERSION}
TOOL_NAME = "service2_connection_test"
TEST_MESSAGE = "TEST DATA — service2 connector check; no financial data was read."
MAX_REQUEST_BYTES = 16 * 1024


def _jsonrpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _response(payload, *, status=200, protocol_version=MCP_PROTOCOL_VERSION):
    response = JsonResponse(payload, status=status)
    response["MCP-Protocol-Version"] = protocol_version
    response["Cache-Control"] = "no-store"
    return response


def _empty_response(*, status, protocol_version=MCP_PROTOCOL_VERSION):
    response = HttpResponse(status=status)
    response["MCP-Protocol-Version"] = protocol_version
    response["Cache-Control"] = "no-store"
    return response


def _request_protocol_version(request):
    requested_version = request.headers.get("MCP-Protocol-Version")
    if requested_version in SUPPORTED_PROTOCOL_VERSIONS:
        return requested_version
    return MCP_PROTOCOL_VERSION


def _tool_definition():
    return {
        "name": TOOL_NAME,
        "title": "Service2 connection test",
        "description": "Returns a fixed test response without reading financial data.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "kind": {"const": "test"},
                "financial_data": {"const": False},
                "message": {"type": "string"},
            },
            "required": ["kind", "financial_data", "message"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    }


@csrf_exempt
def mcp_test(request):
    """Minimal stateless MCP endpoint used only to verify connector access."""

    protocol_version = _request_protocol_version(request)

    if request.method != "POST":
        response = _empty_response(status=405, protocol_version=protocol_version)
        response["Allow"] = "POST"
        return response

    if not settings.ADVISOR_MCP_TEST_ENABLED:
        return _empty_response(status=404, protocol_version=protocol_version)

    origin = request.headers.get("Origin")
    if origin and origin not in settings.ADVISOR_MCP_TEST_ALLOWED_ORIGINS:
        return _empty_response(status=403, protocol_version=protocol_version)

    try:
        content_length = int(request.headers.get("Content-Length", "0"))
    except ValueError:
        return _response(
            _jsonrpc_error(None, -32600, "Invalid Content-Length"),
            status=400,
            protocol_version=protocol_version,
        )
    if content_length > MAX_REQUEST_BYTES:
        return _response(
            _jsonrpc_error(None, -32600, "Request body too large"),
            status=413,
            protocol_version=protocol_version,
        )

    if request.content_type != "application/json":
        return _response(
            _jsonrpc_error(None, -32600, "Content-Type must be application/json"),
            status=415,
            protocol_version=protocol_version,
        )

    accepted_types = {
        item.split(";", 1)[0].strip()
        for item in request.headers.get("Accept", "").split(",")
    }
    if not {"application/json", "text/event-stream"}.issubset(accepted_types):
        return _response(
            _jsonrpc_error(
                None,
                -32600,
                "Accept must include application/json and text/event-stream",
            ),
            status=406,
            protocol_version=protocol_version,
        )

    body = request.body
    if len(body) > MAX_REQUEST_BYTES:
        return _response(
            _jsonrpc_error(None, -32600, "Request body too large"),
            status=413,
            protocol_version=protocol_version,
        )
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return _response(
            _jsonrpc_error(None, -32700, "Parse error"),
            status=400,
            protocol_version=protocol_version,
        )

    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
        request_id = payload.get("id") if isinstance(payload, dict) else None
        return _response(
            _jsonrpc_error(request_id, -32600, "Invalid Request"),
            status=400,
            protocol_version=protocol_version,
        )

    method = payload["method"]
    request_id = payload.get("id")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        return _response(
            _jsonrpc_error(request_id, -32602, "Invalid params"),
            protocol_version=protocol_version,
        )

    if method == "notifications/initialized":
        if "id" not in payload:
            return _empty_response(status=202, protocol_version=protocol_version)
        return _response(
            _jsonrpc_error(request_id, -32600, "notifications/initialized must not include an id"),
            status=400,
            protocol_version=protocol_version,
        )

    if "id" not in payload:
        return _empty_response(status=202, protocol_version=protocol_version)

    if method == "initialize":
        requested_version = params.get("protocolVersion")
        capabilities = params.get("capabilities")
        client_info = params.get("clientInfo")
        if (
            not isinstance(requested_version, str)
            or not isinstance(capabilities, dict)
            or not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not client_info["name"]
            or not isinstance(client_info.get("version"), str)
            or not client_info["version"]
        ):
            return _response(
                _jsonrpc_error(request_id, -32602, "Invalid initialize params"),
                protocol_version=protocol_version,
            )
        protocol_version = (
            requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
        )
        return _response(
            _jsonrpc_result(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "service2-connection-test", "version": "1.0.0"},
                    "instructions": "Test-only connector. It does not expose or read financial data.",
                },
            ),
            protocol_version=protocol_version,
        )

    if request.headers.get("MCP-Protocol-Version") not in SUPPORTED_PROTOCOL_VERSIONS:
        return _response(
            _jsonrpc_error(request_id, -32600, "Unsupported MCP-Protocol-Version"),
            status=400,
            protocol_version=protocol_version,
        )

    if method == "tools/list":
        return _response(
            _jsonrpc_result(request_id, {"tools": [_tool_definition()]}),
            protocol_version=protocol_version,
        )

    if method == "tools/call":
        if params.get("name") != TOOL_NAME:
            return _response(
                _jsonrpc_error(request_id, -32602, "Unknown tool"),
                protocol_version=protocol_version,
            )
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict) or arguments:
            return _response(
                _jsonrpc_error(request_id, -32602, "This tool accepts no arguments"),
                protocol_version=protocol_version,
            )
        structured_content = {
            "kind": "test",
            "financial_data": False,
            "message": "Connector invocation succeeded.",
        }
        return _response(
            _jsonrpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": TEST_MESSAGE}],
                    "structuredContent": structured_content,
                    "isError": False,
                },
            ),
            protocol_version=protocol_version,
        )

    return _response(
        _jsonrpc_error(request_id, -32601, "Method not found"),
        protocol_version=protocol_version,
    )
