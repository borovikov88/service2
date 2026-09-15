"""Protected Streamable HTTP MCP endpoints for live read-only 1C diagnostics."""

from __future__ import annotations

import json
from time import monotonic

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.db import connection
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotFound,
    HttpResponseServerError,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from pool_service import onec_diagnostic
from pool_service.onec_diagnostic import OneCDiagnosticError
from pool_service.onec_diagnostic_mcp_auth import (
    DIAGNOSTIC_READ_SCOPE,
    OneCDiagnosticMcpConfigurationError,
    OneCDiagnosticMcpOAuthError,
    authorization_redirect_uri,
    authorization_server_metadata,
    authenticate_bearer_header,
    diagnostic_mcp_origin_is_allowed,
    exchange_token,
    is_enabled,
    issue_authorization_code,
    protected_resource_metadata,
    protected_resource_metadata_url,
    scoped_organizations,
    validate_authorization_request,
)


MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", MCP_PROTOCOL_VERSION}
MAX_REQUEST_BYTES = 64 * 1024
AUDIT_TABLE = "pool_service_onecdiagnosticmcpauditevent"
TOOL_NAMES = (
    "list_1c_entities",
    "get_1c_entity_schema",
    "read_1c_rows",
)


class DiagnosticToolValidationError(Exception):
    pass


def _no_store(response):
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _mcp_response(payload, *, status=200, protocol_version=MCP_PROTOCOL_VERSION):
    response = JsonResponse(payload, status=status)
    response["MCP-Protocol-Version"] = protocol_version
    return _no_store(response)


def _mcp_empty(*, status, protocol_version=MCP_PROTOCOL_VERSION):
    response = HttpResponse(status=status)
    response["MCP-Protocol-Version"] = protocol_version
    return _no_store(response)


def _jsonrpc_result(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _protocol_version(request):
    requested = request.headers.get("MCP-Protocol-Version")
    return requested if requested in SUPPORTED_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION


def _challenge(*, error=None):
    try:
        metadata_url = protected_resource_metadata_url()
    except OneCDiagnosticMcpConfigurationError:
        metadata_url = ""
    values = [
        'realm="service2-onec-diagnostic"',
        f'resource_metadata="{metadata_url}"' if metadata_url else None,
        f'scope="{DIAGNOSTIC_READ_SCOPE}"',
        f'error="{error}"' if error else None,
    ]
    return "Bearer " + ", ".join(value for value in values if value)


def _authorization_error_response(error, *, protocol_version):
    status = 403 if error.status == 403 else 401
    response = _mcp_response(
        _jsonrpc_error(None, -32001, "Diagnostic MCP требует действующий read-only Bearer token."),
        status=status,
        protocol_version=protocol_version,
    )
    response["WWW-Authenticate"] = _challenge(error=error.error)
    return response


def _accepted_types(request):
    return {
        value.split(";", 1)[0].strip()
        for value in request.headers.get("Accept", "").split(",")
        if value.strip()
    }


def _require_json_mcp_request(request, *, protocol_version):
    try:
        content_length = int(request.headers.get("Content-Length", "0"))
    except ValueError:
        return None, _mcp_response(
            _jsonrpc_error(None, -32600, "Invalid Content-Length"),
            status=400,
            protocol_version=protocol_version,
        )
    if content_length < 0:
        return None, _mcp_response(
            _jsonrpc_error(None, -32600, "Invalid Content-Length"),
            status=400,
            protocol_version=protocol_version,
        )
    if content_length > MAX_REQUEST_BYTES or len(request.body) > MAX_REQUEST_BYTES:
        return None, _mcp_response(
            _jsonrpc_error(None, -32600, "Request body too large"),
            status=413,
            protocol_version=protocol_version,
        )
    if request.content_type != "application/json":
        return None, _mcp_response(
            _jsonrpc_error(None, -32600, "Content-Type must be application/json"),
            status=415,
            protocol_version=protocol_version,
        )
    if "application/json" not in _accepted_types(request):
        return None, _mcp_response(
            _jsonrpc_error(None, -32600, "Accept must include application/json"),
            status=406,
            protocol_version=protocol_version,
        )
    try:
        payload = json.loads(request.body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return None, _mcp_response(
            _jsonrpc_error(None, -32700, "Parse error"),
            status=400,
            protocol_version=protocol_version,
        )
    if (
        not isinstance(payload, dict)
        or payload.get("jsonrpc") != "2.0"
        or not isinstance(payload.get("method"), str)
    ):
        request_id = payload.get("id") if isinstance(payload, dict) else None
        return None, _mcp_response(
            _jsonrpc_error(request_id, -32600, "Invalid Request"),
            status=400,
            protocol_version=protocol_version,
        )
    return payload, None


def _read_only_annotations():
    return {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }


def _tool_definition(name, description, properties, required=()):
    return {
        "name": name,
        "title": name.replace("_", " "),
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        "annotations": _read_only_annotations(),
    }


def _tool_definitions():
    entity_prefixes = [
        "Document_",
        "Catalog_",
        "AccumulationRegister_",
        "InformationRegister_",
        "AccountingRegister_",
    ]
    field_name = {"type": "string", "minLength": 1, "maxLength": 200}
    filter_item = {
        "type": "object",
        "properties": {
            "field": field_name,
            "op": {"type": "string", "enum": ["eq", "ne", "gt", "ge", "lt", "le"]},
            "value": {},
        },
        "required": ["field", "op", "value"],
        "additionalProperties": False,
    }
    return [
        _tool_definition(
            "list_1c_entities",
            "Searches sanitized live 1C OData metadata without reading data rows.",
            {
                "query": {"type": "string", "maxLength": 100},
                "prefix": {"type": "string", "enum": entity_prefixes},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
        ),
        _tool_definition(
            "get_1c_entity_schema",
            "Describes one published business entity and marks sensitive/non-scalar fields.",
            {"entity_set": {"type": "string", "minLength": 1, "maxLength": 300}},
            required=("entity_set",),
        ),
        _tool_definition(
            "read_1c_rows",
            "Reads a small target-organization-scoped slice of one 1C entity through the server policy gateway.",
            {
                "entity_set": {"type": "string", "minLength": 1, "maxLength": 300},
                "fields": {
                    "type": "array",
                    "items": field_name,
                    "minItems": 1,
                    "maxItems": onec_diagnostic.MAX_READ_FIELDS,
                },
                "filters": {
                    "type": "array",
                    "items": filter_item,
                    "minItems": 1,
                    "maxItems": onec_diagnostic.MAX_READ_FILTERS,
                },
                "top": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": onec_diagnostic.MAX_READ_ROWS,
                },
            },
            required=("entity_set", "fields", "filters"),
        ),
    ]


def _reject_unknown_arguments(arguments, allowed):
    if not isinstance(arguments, dict) or set(arguments) - set(allowed):
        raise DiagnosticToolValidationError("Передан неподдерживаемый аргумент инструмента.")


def _tool_dispatch(name, arguments):
    config = onec_diagnostic.config_from_settings()
    if name == "list_1c_entities":
        _reject_unknown_arguments(arguments, {"query", "prefix", "limit"})
        return onec_diagnostic.describe_metadata(
            config,
            query=arguments.get("query", ""),
            prefix=arguments.get("prefix", ""),
            limit=arguments.get("limit", 100),
        )
    if name == "get_1c_entity_schema":
        _reject_unknown_arguments(arguments, {"entity_set"})
        entity_set = arguments.get("entity_set")
        if not isinstance(entity_set, str):
            raise DiagnosticToolValidationError("entity_set обязателен.")
        return onec_diagnostic.get_entity_schema(config, entity_set)
    if name == "read_1c_rows":
        _reject_unknown_arguments(arguments, {"entity_set", "fields", "filters", "top"})
        entity_set = arguments.get("entity_set")
        fields = arguments.get("fields")
        filters = arguments.get("filters")
        if not isinstance(entity_set, str) or not isinstance(fields, list) or not isinstance(filters, list):
            raise DiagnosticToolValidationError("Некорректные structured arguments.")
        # Security and data policy deliberately lives in onec_diagnostic only.
        return onec_diagnostic.read_entity_rows(
            config,
            entity_set,
            fields=fields,
            filters=filters,
            top=arguments.get("top", min(20, config.max_rows)),
        )
    raise DiagnosticToolValidationError("Неизвестный инструмент.")


def _safe_field_names(arguments):
    fields = arguments.get("fields", []) if isinstance(arguments, dict) else []
    if not isinstance(fields, list):
        return []
    return [item[:200] for item in fields[:onec_diagnostic.MAX_READ_FIELDS] if isinstance(item, str)]


def _audit(authenticated, *, name, arguments, result, started, response_bytes=0, required=False):
    """Persist metadata only; never filter values, rows, credentials or tokens."""
    elapsed = max(0, min(int((monotonic() - started) * 1000), 2_147_483_647))
    organization_id = getattr(settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", None)
    try:
        organization_id = int(organization_id)
    except (TypeError, ValueError):
        organization_id = None
    entity_set = ""
    if result == "success" and isinstance(arguments, dict) and isinstance(arguments.get("entity_set"), str):
        entity_set = arguments["entity_set"][:300]
    selected_fields = json.dumps(_safe_field_names(arguments) if result == "success" else [], ensure_ascii=False, separators=(",", ":"))
    values = [
        authenticated.principal.pk if authenticated else None,
        authenticated.grant.pk if authenticated else None,
        authenticated.grant.authorized_by_id if authenticated else None,
        organization_id,
        (name if isinstance(name, str) and name in TOOL_NAMES else "invalid_tool")[:100],
        entity_set,
        selected_fields,
        result[:16],
        elapsed,
        max(0, min(int(response_bytes), 2_147_483_647)),
    ]
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO {AUDIT_TABLE} "
                "(principal_id, grant_id, authorized_by_id, target_organization_id, "
                "tool_name, entity_set, selected_fields, result, duration_ms, response_bytes, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)",
                values,
            )
    except Exception:
        if required:
            raise


@csrf_exempt
def onec_diagnostic_mcp(request):
    protocol_version = _protocol_version(request)
    if not is_enabled():
        return _no_store(HttpResponseNotFound())
    try:
        protected_resource_metadata()
        origin_allowed = diagnostic_mcp_origin_is_allowed(request.headers.get("Origin"))
    except OneCDiagnosticMcpConfigurationError:
        return _mcp_empty(status=503, protocol_version=protocol_version)
    if not origin_allowed:
        return _mcp_empty(status=403, protocol_version=protocol_version)
    if request.method == "OPTIONS":
        response = _mcp_empty(status=204, protocol_version=protocol_version)
        response["Allow"] = "POST, OPTIONS"
        return response
    if request.method != "POST":
        response = _mcp_empty(status=405, protocol_version=protocol_version)
        response["Allow"] = "POST, OPTIONS"
        return response
    try:
        authenticated = authenticate_bearer_header(request.headers.get("Authorization"))
    except OneCDiagnosticMcpOAuthError as exc:
        return _authorization_error_response(exc, protocol_version=protocol_version)
    payload, error_response = _require_json_mcp_request(request, protocol_version=protocol_version)
    if error_response is not None:
        return error_response
    request_id = payload.get("id")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        return _mcp_response(
            _jsonrpc_error(request_id, -32602, "Invalid params"),
            protocol_version=protocol_version,
        )
    method = payload["method"]
    if method == "notifications/initialized":
        if "id" in payload:
            return _mcp_response(
                _jsonrpc_error(request_id, -32600, "notifications/initialized must not include an id"),
                status=400,
                protocol_version=protocol_version,
            )
        return _mcp_empty(status=202, protocol_version=protocol_version)
    if "id" not in payload:
        return _mcp_empty(status=202, protocol_version=protocol_version)
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
            return _mcp_response(
                _jsonrpc_error(request_id, -32602, "Invalid initialize params"),
                protocol_version=protocol_version,
            )
        selected = requested_version if requested_version in SUPPORTED_PROTOCOL_VERSIONS else MCP_PROTOCOL_VERSION
        return _mcp_response(
            _jsonrpc_result(request_id, {
                "protocolVersion": selected,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "service2-onec-diagnostic-readonly", "version": "1.0.0"},
                "instructions": "Controlled read-only live 1C diagnostics. No write/import tools are available.",
            }),
            protocol_version=selected,
        )
    if request.headers.get("MCP-Protocol-Version") not in SUPPORTED_PROTOCOL_VERSIONS:
        return _mcp_response(
            _jsonrpc_error(request_id, -32600, "Unsupported MCP-Protocol-Version"),
            status=400,
            protocol_version=protocol_version,
        )
    if method == "tools/list":
        return _mcp_response(
            _jsonrpc_result(request_id, {"tools": _tool_definitions()}),
            protocol_version=protocol_version,
        )
    if method != "tools/call":
        return _mcp_response(
            _jsonrpc_error(request_id, -32601, "Method not found"),
            protocol_version=protocol_version,
        )
    name = params.get("name")
    arguments = params.get("arguments", {})
    started = monotonic()
    if not isinstance(name, str) or name not in TOOL_NAMES or not isinstance(arguments, dict):
        _audit(authenticated, name=name, arguments=arguments if isinstance(arguments, dict) else {}, result="denied", started=started)
        return _mcp_response(
            _jsonrpc_error(request_id, -32602, "Unknown tool or invalid arguments"),
            protocol_version=protocol_version,
        )
    try:
        data = _tool_dispatch(name, arguments)
        response_bytes = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        _audit(
            authenticated,
            name=name,
            arguments=arguments,
            result="success",
            started=started,
            response_bytes=response_bytes,
            required=True,
        )
    except (DiagnosticToolValidationError, OneCDiagnosticError):
        _audit(authenticated, name=name, arguments=arguments, result="denied", started=started)
        return _mcp_response(
            _jsonrpc_result(request_id, {
                "content": [{"type": "text", "text": "Запрос отклонён серверной политикой Diagnostic MCP."}],
                "isError": True,
            }),
            protocol_version=protocol_version,
        )
    except Exception:
        _audit(authenticated, name=name, arguments=arguments, result="error", started=started)
        return _mcp_response(
            _jsonrpc_result(request_id, {
                "content": [{"type": "text", "text": "Сервер не смог безопасно выполнить Diagnostic запрос."}],
                "isError": True,
            }),
            protocol_version=protocol_version,
        )
    return _mcp_response(
        _jsonrpc_result(request_id, {
            "content": [{"type": "text", "text": "Данные получены через защищённый read-only 1C gateway."}],
            "structuredContent": data,
            "isError": False,
        }),
        protocol_version=protocol_version,
    )


def _metadata_response(factory):
    if not is_enabled():
        return _no_store(HttpResponseNotFound())
    try:
        return _no_store(JsonResponse(factory()))
    except OneCDiagnosticMcpConfigurationError:
        return _no_store(HttpResponseServerError())


def onec_diagnostic_protected_resource_metadata(request):
    if request.method != "GET":
        response = HttpResponse(status=405)
        response["Allow"] = "GET"
        return _no_store(response)
    return _metadata_response(protected_resource_metadata)


def onec_diagnostic_authorization_server_metadata(request):
    if request.method != "GET":
        response = HttpResponse(status=405)
        response["Allow"] = "GET"
        return _no_store(response)
    return _metadata_response(authorization_server_metadata)


def _single_value_params(query_dict):
    if any(len(values) != 1 for _key, values in query_dict.lists()):
        raise OneCDiagnosticMcpOAuthError("invalid_request", "OAuth параметры не должны повторяться.")
    return {key: values[0] for key, values in query_dict.lists()}


def _oauth_error_page(_error):
    return _no_store(HttpResponseBadRequest("OAuth authorization request was rejected."))


@require_http_methods(["GET", "POST"])
def onec_diagnostic_oauth_authorize(request):
    if not is_enabled():
        return _no_store(HttpResponseNotFound())
    if not request.user.is_authenticated:
        return _no_store(redirect_to_login(request.get_full_path()))
    try:
        params = _single_value_params(request.GET if request.method == "GET" else request.POST)
        authorization = validate_authorization_request(params)
    except (OneCDiagnosticMcpOAuthError, OneCDiagnosticMcpConfigurationError) as exc:
        return _oauth_error_page(exc)
    client = authorization["client"]
    from pool_service.onec_diagnostic_mcp_auth import authorization_is_allowed

    if not authorization_is_allowed(request.user, client):
        return _no_store(redirect(authorization_redirect_uri(
            authorization["redirect_uri"], state=authorization["state"], error="access_denied"
        )))
    if request.method == "GET":
        return _no_store(render(request, "pool_service/onec_diagnostic_mcp/authorize.html", {
            "client": client,
            "organizations": scoped_organizations(client),
            "scope": " ".join(authorization["scopes"]),
            "oauth_params": params,
        }))
    if request.POST.get("decision") != "approve":
        return _no_store(redirect(authorization_redirect_uri(
            authorization["redirect_uri"], state=authorization["state"], error="access_denied"
        )))
    try:
        code = issue_authorization_code(authorization=authorization, user=request.user)
    except OneCDiagnosticMcpOAuthError:
        return _no_store(redirect(authorization_redirect_uri(
            authorization["redirect_uri"], state=authorization["state"], error="access_denied"
        )))
    return _no_store(redirect(authorization_redirect_uri(
        authorization["redirect_uri"], code=code, state=authorization["state"]
    )))


@csrf_exempt
@require_http_methods(["POST"])
def onec_diagnostic_oauth_token(request):
    if not is_enabled():
        return _no_store(HttpResponseNotFound())
    if request.content_type != "application/x-www-form-urlencoded":
        return _no_store(JsonResponse(
            {"error": "invalid_request", "error_description": "Content-Type must be application/x-www-form-urlencoded"},
            status=415,
        ))
    try:
        result = exchange_token(_single_value_params(request.POST))
    except OneCDiagnosticMcpOAuthError as exc:
        response = JsonResponse(
            {"error": exc.error, "error_description": exc.description}, status=exc.status
        )
        if exc.status == 401:
            response["WWW-Authenticate"] = 'Basic realm="service2-onec-diagnostic"'
        return _no_store(response)
    except OneCDiagnosticMcpConfigurationError:
        return _no_store(HttpResponseServerError())
    return _no_store(JsonResponse(result))


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MAX_REQUEST_BYTES",
    "TOOL_NAMES",
    "onec_diagnostic_authorization_server_metadata",
    "onec_diagnostic_mcp",
    "onec_diagnostic_oauth_authorize",
    "onec_diagnostic_oauth_token",
    "onec_diagnostic_protected_resource_metadata",
]
