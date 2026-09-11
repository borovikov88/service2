"""HTTP endpoints for the protected, read-only Service2 finance MCP."""

from __future__ import annotations

import json
from datetime import date
from time import monotonic

from django.conf import settings
from django.contrib.auth.views import redirect_to_login
from django.http import (
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseNotFound,
    HttpResponseServerError,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from pool_service.finance_mcp_auth import (
    FINANCE_READ_SCOPE,
    FinanceMcpConfigurationError,
    FinanceMcpOAuthError,
    authorization_redirect_uri,
    authorization_server_metadata,
    authenticate_bearer_header,
    exchange_token,
    finance_mcp_origin_is_allowed,
    is_enabled,
    issue_authorization_code,
    protected_resource_metadata,
    protected_resource_metadata_url,
    scoped_organizations,
    validate_authorization_request,
)
from pool_service.models import FinanceMcpAuditEvent
from pool_service.services.finance_advisor import (
    FinanceAdvisorScopeDenied,
    FinanceAdvisorValidationError,
    get_cashflow_breakdown,
    get_finance_data_status,
    get_monthly_finance,
    get_profit_breakdown,
    organizations_for_principal,
)


MCP_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-03-26", MCP_PROTOCOL_VERSION}
MAX_REQUEST_BYTES = 64 * 1024
TOOL_NAMES = (
    "list_finance_organizations",
    "get_finance_data_status",
    "get_monthly_finance",
    "get_cashflow_breakdown",
    "get_profit_breakdown",
)


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


def _challenge(*, error=None, description=None):
    try:
        metadata_url = protected_resource_metadata_url()
    except FinanceMcpConfigurationError:
        metadata_url = ""
    values = [
        'realm="service2-finance"',
        f'resource_metadata="{metadata_url}"' if metadata_url else None,
        f'scope="{FINANCE_READ_SCOPE}"',
    ]
    if error:
        values.append(f'error="{error}"')
    if description:
        # All descriptions are static code messages; quote defensively anyway.
        values.append('error_description="' + description.replace('"', "'") + '"')
    return "Bearer " + ", ".join(value for value in values if value)


def _authorization_error_response(error, *, protocol_version):
    status = 403 if error.status == 403 else 401
    response = _mcp_response(
        _jsonrpc_error(None, -32001, "Финансовый MCP требует действующий read-only Bearer token."),
        status=status,
        protocol_version=protocol_version,
    )
    response["WWW-Authenticate"] = _challenge(
        error=error.error,
        # HTTP authentication parameters must remain ASCII.  The JSON-RPC
        # response deliberately carries the human-safe Russian explanation;
        # placing it in WWW-Authenticate would make Django RFC-encode the
        # header and break OAuth/MCP clients' Bearer parser.
        description=None,
    )
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
    accepted = _accepted_types(request)
    if "application/json" not in accepted:
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
    organization_ids = {
        "type": "array",
        "items": {"type": "integer", "minimum": 1},
        "minItems": 1,
        "maxItems": 50,
        "description": "Необязательный полный список ID разрешённых организаций.",
    }
    month = {"type": "string", "pattern": "^[0-9]{4}-(0[1-9]|1[0-2])$"}
    page = {"type": "integer", "minimum": 1, "maximum": 10000}
    page_size = {"type": "integer", "minimum": 1, "maximum": 100}
    return [
        _tool_definition(
            "list_finance_organizations",
            "Lists only organizations available to the authenticated financial read identity.",
            {},
        ),
        _tool_definition(
            "get_finance_data_status",
            "Returns active confirmed finance-source coverage, freshness and completeness warnings.",
            {"organization_ids": organization_ids},
        ),
        _tool_definition(
            "get_monthly_finance",
            "Returns exact monthly profit, payroll and classified cash-flow metrics from the shared server calculation.",
            {
                "start_month": month,
                "end_month": month,
                "organization_ids": organization_ids,
            },
            required=("start_month", "end_month"),
        ),
        _tool_definition(
            "get_cashflow_breakdown",
            "Returns paginated classified cash-flow receipts, payments and net flow from active confirmed versions.",
            {
                "start_month": month,
                "end_month": month,
                "organization_ids": organization_ids,
                "group_by": {
                    "type": "string",
                    "enum": ["month", "flow_type", "management_category", "article"],
                },
                "flow_types": {
                    "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 6,
                },
                "management_categories": {
                    "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50,
                },
                "page": page,
                "page_size": page_size,
            },
            required=("start_month", "end_month", "group_by"),
        ),
        _tool_definition(
            "get_profit_breakdown",
            "Returns paginated gross-profit breakdown by a real source dimension; department and object report unavailable rather than inventing a split.",
            {
                "start_month": month,
                "end_month": month,
                "organization_ids": organization_ids,
                "group_by": {
                    "type": "string",
                    "enum": ["month", "manager", "customer", "nomenclature", "document", "department", "object"],
                },
                "page": page,
                "page_size": page_size,
            },
            required=("start_month", "end_month", "group_by"),
        ),
    ]


def _reject_unknown_arguments(arguments, allowed):
    unknown = set(arguments) - set(allowed)
    if unknown:
        raise FinanceAdvisorValidationError("Передан неподдерживаемый аргумент инструмента.")


def _as_month(value, field):
    if not isinstance(value, str):
        raise FinanceAdvisorValidationError(f"{field} обязателен и должен иметь формат YYYY-MM.")
    return value


def _as_optional_organization_ids(arguments):
    return arguments.get("organization_ids")


def _tool_dispatch(principal, name, arguments):
    if name == "list_finance_organizations":
        _reject_unknown_arguments(arguments, set())
        organizations = organizations_for_principal(principal)
        return {
            "as_of": timezone.now().isoformat(),
            "period": {"from": None, "to": None},
            "organizations": [
                {"id": organization.pk, "name": organization.name}
                for organization in organizations
            ],
            "complete": True,
            "warnings": [],
            "missing_months": [],
            "preliminary_months": [],
            "active_batch_ids": [],
            "active_versions": [],
            "source": {
                "source_scope": "finance_mcp_principal_organization_scope",
            },
        }
    if name == "get_finance_data_status":
        _reject_unknown_arguments(arguments, {"organization_ids"})
        return get_finance_data_status(principal, _as_optional_organization_ids(arguments))
    if name == "get_monthly_finance":
        _reject_unknown_arguments(arguments, {"start_month", "end_month", "organization_ids"})
        return get_monthly_finance(
            principal,
            _as_month(arguments.get("start_month"), "start_month"),
            _as_month(arguments.get("end_month"), "end_month"),
            _as_optional_organization_ids(arguments),
        )
    if name == "get_cashflow_breakdown":
        _reject_unknown_arguments(arguments, {
            "start_month", "end_month", "organization_ids", "group_by", "flow_types",
            "management_categories", "page", "page_size",
        })
        group_by = arguments.get("group_by")
        if not isinstance(group_by, str):
            raise FinanceAdvisorValidationError("group_by обязателен.")
        return get_cashflow_breakdown(
            principal,
            _as_month(arguments.get("start_month"), "start_month"),
            _as_month(arguments.get("end_month"), "end_month"),
            organization_ids=_as_optional_organization_ids(arguments),
            group_by=group_by,
            flow_types=arguments.get("flow_types"),
            management_categories=arguments.get("management_categories"),
            page=arguments.get("page", 1),
            page_size=arguments.get("page_size", 50),
        )
    if name == "get_profit_breakdown":
        _reject_unknown_arguments(arguments, {
            "start_month", "end_month", "organization_ids", "group_by", "page", "page_size",
        })
        group_by = arguments.get("group_by")
        if not isinstance(group_by, str):
            raise FinanceAdvisorValidationError("group_by обязателен.")
        return get_profit_breakdown(
            principal,
            _as_month(arguments.get("start_month"), "start_month"),
            _as_month(arguments.get("end_month"), "end_month"),
            organization_ids=_as_optional_organization_ids(arguments),
            group_by=group_by,
            page=arguments.get("page", 1),
            page_size=arguments.get("page_size", 50),
        )
    raise FinanceAdvisorValidationError("Неизвестный read-only финансовый инструмент.")


def _audit_values(name, arguments):
    organization_ids = arguments.get("organization_ids", []) if isinstance(arguments, dict) else []
    if not isinstance(organization_ids, list):
        organization_ids = []
    first = arguments.get("start_month") if isinstance(arguments, dict) else None
    last = arguments.get("end_month") if isinstance(arguments, dict) else None
    try:
        period_start = date.fromisoformat(f"{first}-01") if isinstance(first, str) else None
        period_end = date.fromisoformat(f"{last}-01") if isinstance(last, str) else None
    except ValueError:
        period_start = period_end = None
    group_by = arguments.get("group_by", "") if isinstance(arguments, dict) else ""
    return organization_ids, period_start, period_end, group_by if isinstance(group_by, str) else ""


def _response_organization_ids(data):
    """Take audited organization IDs only from the canonical response envelope."""
    if not isinstance(data, dict):
        return None
    organizations = data.get("organizations")
    if not isinstance(organizations, list):
        return None
    identifiers = []
    for organization in organizations:
        identifier = organization.get("id") if isinstance(organization, dict) else None
        if isinstance(identifier, int) and not isinstance(identifier, bool) and identifier > 0:
            identifiers.append(identifier)
        else:
            return None
    return list(dict.fromkeys(identifiers))


def _audit(
    authenticated,
    *,
    name,
    arguments,
    result,
    started,
    response_bytes=0,
    resolved_organization_ids=None,
):
    organization_ids, period_start, period_end, group_by = _audit_values(name, arguments)
    if resolved_organization_ids is not None:
        organization_ids = list(dict.fromkeys(resolved_organization_ids))
    elif not organization_ids:
        # The omitted filter means the complete server-side scope.  A denied
        # call must preserve that audit fact too, without returning the scope
        # to the MCP client.
        organization_ids = list(
            authenticated.principal.organization_scopes.order_by("organization_id")
            .values_list("organization_id", flat=True)
        )
    elapsed = max(0, round((monotonic() - started) * 1000))
    FinanceMcpAuditEvent.objects.create(
        principal=authenticated.principal,
        grant=authenticated.grant,
        tool_name=name[:100],
        organization_ids=organization_ids,
        period_start=period_start,
        period_end=period_end,
        group_by=group_by[:64],
        result=result,
        duration_ms=elapsed,
        response_bytes=max(0, min(int(response_bytes), 2_147_483_647)),
    )


@csrf_exempt
def finance_mcp(request):
    """Stateless Streamable HTTP MCP endpoint with mandatory Bearer OAuth."""
    protocol_version = _protocol_version(request)
    if not is_enabled():
        return _no_store(HttpResponseNotFound())
    try:
        # Fail closed rather than trusting Host headers when production forgot
        # the canonical HTTPS resource/issuer configuration.
        protected_resource_metadata()
        origin_allowed = finance_mcp_origin_is_allowed(request.headers.get("Origin"))
    except FinanceMcpConfigurationError:
        return _mcp_empty(status=503, protocol_version=protocol_version)
    if not origin_allowed:
        # Do not echo Origin or add permissive CORS headers.  The streamable
        # transport may be server-to-server (no Origin), but a present browser
        # Origin must be an exact configured value before Bearer/body handling.
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
    except FinanceMcpOAuthError as exc:
        # Authenticate before parsing any JSON-RPC body.  A first MCP POST is
        # therefore always a proper OAuth Bearer challenge, never a login page
        # or a protocol/data side channel.
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
                "serverInfo": {"name": "service2-finance-readonly", "version": "1.0.0"},
                "instructions": "Read-only financial data from active confirmed Service2 sources. No import or write tools are available.",
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
        _audit(
            authenticated,
            name=name if isinstance(name, str) else "invalid_tool",
            arguments=arguments if isinstance(arguments, dict) else {},
            result=FinanceMcpAuditEvent.RESULT_DENIED,
            started=started,
        )
        return _mcp_response(
            _jsonrpc_error(request_id, -32602, "Unknown tool or invalid arguments"),
            protocol_version=protocol_version,
        )
    try:
        data = _tool_dispatch(authenticated.principal, name, arguments)
        response_bytes = len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        _audit(
            authenticated,
            name=name,
            arguments=arguments,
            result=FinanceMcpAuditEvent.RESULT_SUCCESS,
            started=started,
            response_bytes=response_bytes,
            resolved_organization_ids=_response_organization_ids(data),
        )
    except (FinanceAdvisorScopeDenied, FinanceAdvisorValidationError) as exc:
        _audit(
            authenticated,
            name=name,
            arguments=arguments,
            result=FinanceMcpAuditEvent.RESULT_DENIED,
            started=started,
        )
        return _mcp_response(
            _jsonrpc_result(request_id, {
                "content": [{"type": "text", "text": "Запрос отклонён серверной политикой финансового доступа."}],
                "isError": True,
            }),
            protocol_version=protocol_version,
        )
    except Exception:
        # The audit contains only operation metadata; never copy exception
        # text because database/financial details can be sensitive.
        _audit(
            authenticated,
            name=name,
            arguments=arguments,
            result=FinanceMcpAuditEvent.RESULT_ERROR,
            started=started,
        )
        return _mcp_response(
            _jsonrpc_result(request_id, {
                "content": [{"type": "text", "text": "Сервер не смог безопасно выполнить финансовый запрос."}],
                "isError": True,
            }),
            protocol_version=protocol_version,
        )
    return _mcp_response(
        _jsonrpc_result(request_id, {
            "content": [{"type": "text", "text": "Данные получены из активных подтверждённых источников Service2."}],
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
    except FinanceMcpConfigurationError:
        return _no_store(HttpResponseServerError())


def finance_protected_resource_metadata(request):
    if request.method != "GET":
        response = HttpResponse(status=405)
        response["Allow"] = "GET"
        return _no_store(response)
    return _metadata_response(protected_resource_metadata)


def finance_authorization_server_metadata(request):
    if request.method != "GET":
        response = HttpResponse(status=405)
        response["Allow"] = "GET"
        return _no_store(response)
    return _metadata_response(authorization_server_metadata)


def _oauth_error_page(error):
    response = HttpResponseBadRequest("OAuth authorization request was rejected.")
    return _no_store(response)


def _single_value_params(query_dict):
    """Reject ambiguous OAuth parameters instead of silently selecting one.

    Django's ``QueryDict.dict`` would retain only the final duplicate.  OAuth
    parameters influence client selection and code delivery, so ambiguity is a
    fail-closed invalid request rather than a convenience conversion.
    """
    if any(len(values) != 1 for _key, values in query_dict.lists()):
        raise FinanceMcpOAuthError("invalid_request", "OAuth параметры не должны повторяться.")
    return {key: values[0] for key, values in query_dict.lists()}


@require_http_methods(["GET", "POST"])
def finance_oauth_authorize(request):
    """Human owner approval screen for one pre-registered finance client."""
    if not is_enabled():
        return _no_store(HttpResponseNotFound())
    if not request.user.is_authenticated:
        # Keep the complete OAuth request (client, state, PKCE and resource)
        # as Django's validated login ``next`` value.  The global middleware
        # deliberately allows this route through so it cannot discard query
        # parameters before this point.
        return _no_store(redirect_to_login(request.get_full_path()))
    try:
        params = _single_value_params(request.GET if request.method == "GET" else request.POST)
        authorization = validate_authorization_request(params)
    except (FinanceMcpOAuthError, FinanceMcpConfigurationError) as exc:
        return _oauth_error_page(exc)
    client = authorization["client"]
    from pool_service.finance_mcp_auth import authorization_is_allowed

    if not authorization_is_allowed(request.user, client):
        # The redirect URI and state were already fully validated.  OAuth
        # clients that opt into ``authorization_response_iss_parameter`` need
        # a normal authorization error response (including ``iss``), rather
        # than a bare HTML 403 that loses state and cannot be correlated.
        return _no_store(redirect(authorization_redirect_uri(
            authorization["redirect_uri"],
            state=authorization["state"],
            error="access_denied",
        )))
    if request.method == "GET":
        return _no_store(render(request, "pool_service/finance_mcp/authorize.html", {
            "client": client,
            "organizations": scoped_organizations(client),
            "scope": " ".join(authorization["scopes"]),
            "oauth_params": params,
        }))
    if request.POST.get("decision") != "approve":
        return _no_store(redirect(authorization_redirect_uri(
            authorization["redirect_uri"],
            state=authorization["state"],
            error="access_denied",
        )))
    try:
        code = issue_authorization_code(authorization=authorization, user=request.user)
    except FinanceMcpOAuthError:
        # Authorization was valid but its server-side guard changed between
        # preview and approval.  Preserve the OAuth response contract without
        # disclosing internal authorization details to the redirect URI.
        return _no_store(redirect(authorization_redirect_uri(
            authorization["redirect_uri"],
            state=authorization["state"],
            error="access_denied",
        )))
    return _no_store(redirect(authorization_redirect_uri(
        authorization["redirect_uri"], code=code, state=authorization["state"]
    )))


@csrf_exempt
@require_http_methods(["POST"])
def finance_oauth_token(request):
    """OAuth token endpoint.  It is form-only and never accepts a URL token."""
    if not is_enabled():
        return _no_store(HttpResponseNotFound())
    if request.content_type != "application/x-www-form-urlencoded":
        response = JsonResponse({"error": "invalid_request", "error_description": "Content-Type must be application/x-www-form-urlencoded"}, status=415)
        return _no_store(response)
    try:
        result = exchange_token(_single_value_params(request.POST))
    except FinanceMcpOAuthError as exc:
        response = JsonResponse(
            {"error": exc.error, "error_description": exc.description}, status=exc.status
        )
        if exc.status == 401:
            response["WWW-Authenticate"] = 'Basic realm="service2-finance"'
        return _no_store(response)
    except FinanceMcpConfigurationError:
        return _no_store(HttpResponseServerError())
    return _no_store(JsonResponse(result))


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MAX_REQUEST_BYTES",
    "TOOL_NAMES",
    "finance_authorization_server_metadata",
    "finance_mcp",
    "finance_oauth_authorize",
    "finance_oauth_token",
    "finance_protected_resource_metadata",
]
