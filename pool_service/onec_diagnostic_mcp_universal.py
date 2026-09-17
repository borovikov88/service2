"""Install the universal read-only 1C query surface on the reviewed MCP stack."""

from __future__ import annotations

from contextvars import ContextVar

from pool_service import onec_diagnostic as base
from pool_service import onec_diagnostic_universal as universal

# Install role tightening and the live Double sales-schema compatibility before
# the OAuth/transport modules import and evaluate the Diagnostic core.
universal.install_universal_policy()

from pool_service import onec_diagnostic_mcp_auth as auth  # noqa: E402
from pool_service import onec_diagnostic_mcp_views as legacy  # noqa: E402

# Tighten authorization for this MCP without changing the legacy core helper's
# compatibility semantics used elsewhere. Auth functions resolve this global
# at call time, including token revalidation.
auth.can_access_diagnostic_mcp = universal.can_access_universal_diagnostic_mcp


_error_code: ContextVar[str | None] = ContextVar(
    "onec_diagnostic_error_code", default=None
)
_original_tool_definitions = legacy._tool_definitions
_original_tool_dispatch = legacy._tool_dispatch
_original_mcp_response = legacy._mcp_response


def _universal_tool_definition():
    field_name = {"type": "string", "minLength": 1, "maxLength": 200}
    filter_item = {
        "type": "object",
        "properties": {
            "field": field_name,
            "op": {
                "type": "string",
                "enum": sorted(universal.QUERY_FILTER_OPERATORS),
            },
            "value": {},
        },
        "required": ["field", "op", "value"],
        "additionalProperties": False,
    }
    order_item = {
        "type": "object",
        "properties": {
            "field": field_name,
            "direction": {"type": "string", "enum": ["asc", "desc"]},
        },
        "required": ["field", "direction"],
        "additionalProperties": False,
    }
    return legacy._tool_definition(
        "query_1c_rows",
        (
            "Runs a reusable metadata-driven read-only 1C query using only "
            "structured fields, filters and ordering. No raw OData is accepted."
        ),
        {
            "entity_set": {"type": "string", "minLength": 1, "maxLength": 300},
            "fields": {
                "type": "array",
                "items": field_name,
                "minItems": 1,
                "maxItems": base.MAX_READ_FIELDS,
            },
            "filters": {
                "type": "array",
                "items": filter_item,
                "minItems": 1,
                "maxItems": base.MAX_READ_FILTERS,
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": universal.MAX_QUERY_ROWS,
            },
            "include_deleted": {"type": "boolean"},
            "include_inactive": {"type": "boolean"},
            "order_by": {
                "type": "array",
                "items": order_item,
                "maxItems": universal.MAX_QUERY_ORDER_FIELDS,
            },
        },
        required=("entity_set", "fields", "filters"),
    )


def _tool_definitions():
    tools = _original_tool_definitions()
    if not any(tool.get("name") == "query_1c_rows" for tool in tools):
        tools.append(_universal_tool_definition())
    return tools


def _tool_dispatch(name, arguments):
    _error_code.set(None)
    try:
        if name != "query_1c_rows":
            return _original_tool_dispatch(name, arguments)
        legacy._reject_unknown_arguments(
            arguments,
            {
                "entity_set",
                "fields",
                "filters",
                "limit",
                "include_deleted",
                "include_inactive",
                "order_by",
            },
        )
        entity_set = arguments.get("entity_set")
        fields = arguments.get("fields")
        filters = arguments.get("filters")
        if (
            not isinstance(entity_set, str)
            or not isinstance(fields, list)
            or not isinstance(filters, list)
        ):
            raise legacy.DiagnosticToolValidationError(
                "Некорректные structured arguments."
            )
        return universal.query_1c_rows(
            base.config_from_settings(),
            entity_set,
            fields=fields,
            filters=filters,
            limit=arguments.get("limit", 100),
            include_deleted=arguments.get("include_deleted", False),
            include_inactive=arguments.get("include_inactive", False),
            order_by=arguments.get("order_by"),
        )
    except base.OneCDiagnosticError as exc:
        _error_code.set(exc.code)
        raise


def _mcp_response(payload, *, status=200, protocol_version=legacy.MCP_PROTOCOL_VERSION):
    code = _error_code.get()
    if code and isinstance(payload, dict):
        result = payload.get("result")
        if isinstance(result, dict) and result.get("isError") is True:
            content = result.get("content")
            if (
                isinstance(content, list)
                and content
                and isinstance(content[0], dict)
                and content[0].get("type") == "text"
            ):
                content[0] = {
                    "type": "text",
                    "text": f"Diagnostic MCP отклонил запрос. Код: {code}.",
                }
            result["structuredContent"] = {"error_code": code}
            _error_code.set(None)
    return _original_mcp_response(
        payload, status=status, protocol_version=protocol_version
    )


if "query_1c_rows" not in legacy.TOOL_NAMES:
    legacy.TOOL_NAMES = (*legacy.TOOL_NAMES, "query_1c_rows")
legacy._tool_definitions = _tool_definitions
legacy._tool_dispatch = _tool_dispatch
legacy._mcp_response = _mcp_response

# The ChatGPT-facing adapter imports this already-patched legacy module later.
# Keeping this installer transport-agnostic avoids changing URL routing or OAuth topology.

__all__ = []
