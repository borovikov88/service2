"""Bounded, read-only 1C OData diagnostics for future MCP tools.

This module deliberately does not expose an HTTP/MCP endpoint.  It provides a
small server-side contract that can inspect published OData metadata and read a
strictly bounded set of rows.  The external diagnostic MCP can be layered on
this contract after its own authorization model is reviewed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import re
from typing import Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, build_opener
from uuid import UUID
import xml.etree.ElementTree as ET

from django.conf import settings

from pool_service.finance_imports.odata_profit import (
    NoRedirectHandler,
    ODataConfig,
    ODataPreviewError,
    read_odata_pages,
    validate_config,
)


MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_ENTITY_SETS = 10000
MAX_PROPERTIES_PER_ENTITY = 1000
MAX_LIST_ENTITIES = 200
MAX_READ_FIELDS = 40
MAX_READ_FILTERS = 8
MAX_READ_ROWS = 50

EDM_NAMESPACES = {
    "http://schemas.microsoft.com/ado/2006/04/edm",
    "http://schemas.microsoft.com/ado/2007/05/edm",
    "http://schemas.microsoft.com/ado/2008/09/edm",
    "http://schemas.microsoft.com/ado/2009/11/edm",
    "http://docs.oasis-open.org/odata/ns/edm",
}
IDENTIFIER_RE = re.compile(r"[^\W\d]\w*(?:\.[^\W\d]\w*)*\Z", re.UNICODE)
READABLE_ENTITY_PREFIXES = (
    "Document_",
    "Catalog_",
    "AccumulationRegister_",
    "InformationRegister_",
    "AccountingRegister_",
)
FILTER_OPERATORS = frozenset({"eq", "ne", "gt", "ge", "lt", "le"})
SENSITIVE_FIELD_RE = re.compile(
    r"password|парол|secret|секрет|token|токен|credential|"
    r"api[_ ]?key|access[_ ]?key|private[_ ]?key",
    re.IGNORECASE,
)


class OneCDiagnosticError(Exception):
    """Fixed diagnostic failure that is safe to expose without source payloads."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class EntitySchema:
    name: str
    entity_type: str
    properties: tuple[tuple[str, str], ...]

    @property
    def field_types(self) -> dict[str, str]:
        return dict(self.properties)


def config_from_settings() -> ODataConfig:
    """Use the existing 1C connection without creating another credential path."""
    return ODataConfig(
        base_url=settings.ONEC_ODATA_BASE_URL,
        username=settings.ONEC_ODATA_USERNAME,
        password=settings.ONEC_ODATA_PASSWORD,
        organization_guids=tuple(settings.ONEC_ODATA_ORGANIZATION_GUIDS),
        timeout_seconds=settings.ONEC_ODATA_TIMEOUT_SECONDS,
        max_pages=min(int(settings.ONEC_ODATA_MAX_PAGES), 20),
        max_rows=min(int(settings.ONEC_ODATA_MAX_ROWS), MAX_READ_ROWS),
    )


def _validated_config(config: ODataConfig) -> ODataConfig:
    try:
        validated = validate_config(config)
    except ODataPreviewError as exc:
        raise OneCDiagnosticError("INVALID_ODATA_CONFIG") from exc
    if urlsplit(validated.base_url).scheme.lower() != "https":
        raise OneCDiagnosticError("DIAGNOSTIC_ODATA_REQUIRES_HTTPS")
    if ":" in validated.username:
        raise OneCDiagnosticError("INVALID_ODATA_CREDENTIAL_CONFIG")
    return validated


def _local_tag(element) -> str:
    if not isinstance(element.tag, str) or not element.tag.startswith("{"):
        return ""
    namespace, tag = element.tag[1:].split("}", 1)
    return tag if namespace in EDM_NAMESPACES else ""


def _identifier(value) -> str:
    if not isinstance(value, str) or not IDENTIFIER_RE.fullmatch(value):
        raise OneCDiagnosticError("INVALID_SCHEMA_IDENTIFIER")
    return value


def _type_name(value) -> str:
    if isinstance(value, str) and value.startswith("Collection(") and value.endswith(")"):
        return _identifier(value[11:-1])
    return _identifier(value)


def fetch_metadata(config: ODataConfig, *, opener=None) -> bytes:
    """Fetch `$metadata` over a bounded GET-only HTTPS request."""
    config = _validated_config(config)
    request = Request(
        config.base_url + "$metadata",
        headers={"Accept": "application/xml"},
        method="GET",
    )
    if config.username:
        token = base64.b64encode(
            f"{config.username}:{config.password}".encode("utf-8")
        ).decode("ascii")
        request.add_header("Authorization", f"Basic {token}")
    client = opener or build_opener(NoRedirectHandler())
    try:
        with client.open(request, timeout=config.timeout_seconds) as response:
            if getattr(response, "status", 200) != 200:
                raise OneCDiagnosticError("METADATA_HTTP_NOT_200")
            raw = response.read(MAX_METADATA_BYTES + 1)
    except OneCDiagnosticError:
        raise
    except HTTPError as exc:
        raise OneCDiagnosticError("METADATA_HTTP_ERROR") from exc
    except URLError as exc:
        raise OneCDiagnosticError("METADATA_REQUEST_FAILED") from exc
    if len(raw) > MAX_METADATA_BYTES:
        raise OneCDiagnosticError("METADATA_SIZE_LIMIT")
    return raw


def parse_metadata(raw: bytes) -> dict[str, EntitySchema]:
    """Return a sanitized entity/field index from an EDMX document."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > MAX_METADATA_BYTES:
        raise OneCDiagnosticError("METADATA_SIZE_LIMIT")
    try:
        xml = bytes(raw).decode("utf-8-sig")
    except UnicodeError as exc:
        raise OneCDiagnosticError("METADATA_REQUIRES_UTF8") from exc
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.IGNORECASE):
        raise OneCDiagnosticError("METADATA_DTD_FORBIDDEN")
    try:
        root = ET.fromstring(xml)
    except (ET.ParseError, ValueError) as exc:
        raise OneCDiagnosticError("METADATA_INVALID_XML") from exc

    schemas = [node for node in root.iter() if _local_tag(node) == "Schema"]
    if not schemas:
        raise OneCDiagnosticError("METADATA_EDM_SCHEMA_MISSING")

    types: dict[str, object] = {}
    aliases: dict[str, str] = {}
    entity_sets: dict[str, str] = {}
    for schema in schemas:
        namespace = _identifier(schema.get("Namespace"))
        if schema.get("Alias"):
            aliases[_identifier(schema.get("Alias"))] = namespace
        for node in schema:
            kind = _local_tag(node)
            if kind in {"EntityType", "ComplexType"}:
                qualified = namespace + "." + _identifier(node.get("Name"))
                if qualified in types:
                    raise OneCDiagnosticError("METADATA_DUPLICATE_TYPE")
                types[qualified] = node
            elif kind == "EntityContainer":
                for item in node:
                    if _local_tag(item) != "EntitySet":
                        continue
                    name = _identifier(item.get("Name"))
                    if name in entity_sets:
                        raise OneCDiagnosticError("METADATA_DUPLICATE_ENTITY_SET")
                    entity_sets[name] = _type_name(item.get("EntityType"))
                    if len(entity_sets) > MAX_ENTITY_SETS:
                        raise OneCDiagnosticError("METADATA_ENTITY_LIMIT")

    def canonical(value: str) -> str:
        name = _type_name(value)
        prefix, dot, suffix = name.partition(".")
        return aliases.get(prefix, prefix) + dot + suffix

    result: dict[str, EntitySchema] = {}
    for entity_name, declared_type in entity_sets.items():
        qualified = canonical(declared_type)
        current = qualified
        seen: set[str] = set()
        fields: dict[str, str] = {}
        while current and current not in seen:
            seen.add(current)
            node = types.get(current)
            if node is None:
                raise OneCDiagnosticError("METADATA_TYPE_REFERENCE_MISSING")
            for child in node:
                if _local_tag(child) != "Property":
                    continue
                field_name = _identifier(child.get("Name"))
                declared = child.get("Type")
                if not isinstance(declared, str):
                    raise OneCDiagnosticError("METADATA_TYPE_REFERENCE_MISSING")
                fields.setdefault(field_name, declared)
                if len(fields) > MAX_PROPERTIES_PER_ENTITY:
                    raise OneCDiagnosticError("METADATA_PROPERTY_LIMIT")
            current = canonical(node.get("BaseType")) if node.get("BaseType") else ""
        result[entity_name] = EntitySchema(
            name=entity_name,
            entity_type=qualified,
            properties=tuple(sorted(fields.items())),
        )
    return result


def _is_readable_entity(name: str) -> bool:
    return any(name.startswith(prefix) for prefix in READABLE_ENTITY_PREFIXES)


def describe_metadata(
    config: ODataConfig,
    *,
    query: str = "",
    prefix: str = "",
    limit: int = 100,
    opener=None,
    metadata_raw: bytes | None = None,
):
    """List published business entity sets without returning any 1C rows."""
    config = _validated_config(config)
    if not isinstance(query, str) or len(query) > 100:
        raise OneCDiagnosticError("INVALID_METADATA_QUERY")
    if prefix and prefix not in READABLE_ENTITY_PREFIXES:
        raise OneCDiagnosticError("INVALID_ENTITY_PREFIX")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_ENTITIES:
        raise OneCDiagnosticError("INVALID_ENTITY_LIMIT")
    raw = metadata_raw if metadata_raw is not None else fetch_metadata(config, opener=opener)
    index = parse_metadata(raw)
    needle = query.casefold().strip()
    matched = [
        schema for schema in index.values()
        if _is_readable_entity(schema.name)
        and (not prefix or schema.name.startswith(prefix))
        and (not needle or needle in schema.name.casefold()
            or any(needle in field.casefold() for field, _ in schema.properties))
    ]
    matched.sort(key=lambda item: item.name)
    selected = matched[:limit]
    return {
        "kind": "onec_diagnostic_metadata",
        "entity_count": len(matched),
        "entities": [
            {
                "name": item.name,
                "field_count": len(item.properties),
                "fields": [name for name, _declared in item.properties],
            }
            for item in selected
        ],
        "truncated": len(matched) > len(selected),
        "source": "live_1c_metadata" if metadata_raw is None else "provided_metadata",
    }


def get_entity_schema(
    config: ODataConfig,
    entity_set: str,
    *,
    opener=None,
    metadata_raw: bytes | None = None,
):
    """Describe one published entity set, without reading its data rows."""
    config = _validated_config(config)
    if not isinstance(entity_set, str) or not IDENTIFIER_RE.fullmatch(entity_set):
        raise OneCDiagnosticError("INVALID_ENTITY_SET")
    if not _is_readable_entity(entity_set):
        raise OneCDiagnosticError("ENTITY_SET_NOT_ALLOWED")
    raw = metadata_raw if metadata_raw is not None else fetch_metadata(config, opener=opener)
    schema = parse_metadata(raw).get(entity_set)
    if schema is None:
        raise OneCDiagnosticError("ENTITY_SET_NOT_PUBLISHED")
    return {
        "name": schema.name,
        "entity_type": schema.entity_type,
        "fields": [
            {"name": name, "type": declared, "sensitive": bool(SENSITIVE_FIELD_RE.search(name))}
            for name, declared in schema.properties
        ],
    }


def _require_safe_field(name: str, field_types: Mapping[str, str]) -> str:
    if not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name) or "." in name:
        raise OneCDiagnosticError("INVALID_FIELD")
    if name not in field_types:
        raise OneCDiagnosticError("FIELD_NOT_PUBLISHED")
    if SENSITIVE_FIELD_RE.search(name):
        raise OneCDiagnosticError("SENSITIVE_FIELD_DENIED")
    return name


def _string_literal(value) -> str:
    if not isinstance(value, str) or len(value) > 300:
        raise OneCDiagnosticError("INVALID_FILTER_VALUE")
    return "'" + value.replace("'", "''") + "'"


def _datetime_literal(value, *, offset: bool = False) -> str:
    if isinstance(value, date) and not isinstance(value, datetime):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and len(value) <= 40:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OneCDiagnosticError("INVALID_FILTER_VALUE") from exc
    else:
        raise OneCDiagnosticError("INVALID_FILTER_VALUE")
    if offset:
        rendered = parsed.isoformat(timespec="seconds")
        return f"datetimeoffset'{rendered}'"
    if parsed.tzinfo is not None:
        raise OneCDiagnosticError("INVALID_FILTER_VALUE")
    return f"datetime'{parsed.isoformat(timespec='seconds')}'"


def _odata_literal(declared_type: str, value) -> str:
    if declared_type == "Edm.String":
        return _string_literal(value)
    if declared_type == "Edm.Guid":
        try:
            return f"guid'{str(UUID(str(value))).lower()}'"
        except (TypeError, ValueError, AttributeError) as exc:
            raise OneCDiagnosticError("INVALID_FILTER_VALUE") from exc
    if declared_type == "Edm.Boolean":
        if type(value) is not bool:
            raise OneCDiagnosticError("INVALID_FILTER_VALUE")
        return "true" if value else "false"
    if declared_type == "Edm.DateTime":
        return _datetime_literal(value)
    if declared_type == "Edm.DateTimeOffset":
        return _datetime_literal(value, offset=True)
    if declared_type in {"Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte", "Edm.SByte"}:
        if isinstance(value, bool):
            raise OneCDiagnosticError("INVALID_FILTER_VALUE")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise OneCDiagnosticError("INVALID_FILTER_VALUE") from exc
        if str(parsed) != str(value).strip():
            raise OneCDiagnosticError("INVALID_FILTER_VALUE")
        return str(parsed)
    raise OneCDiagnosticError("FILTER_TYPE_NOT_SUPPORTED")


def _is_selective_filter(field: str) -> bool:
    return field in {"Ref_Key", "Number", "Date", "Period"} or field.endswith("_Key")


def _build_filter_expression(
    filters: Iterable[Mapping[str, object]],
    field_types: Mapping[str, str],
    organization_guids: tuple[str, ...],
) -> str:
    filters = list(filters)
    if not filters or len(filters) > MAX_READ_FILTERS:
        raise OneCDiagnosticError("FILTER_COUNT_OUT_OF_RANGE")
    clauses = []
    selective = False
    for item in filters:
        if not isinstance(item, Mapping) or set(item) != {"field", "op", "value"}:
            raise OneCDiagnosticError("INVALID_FILTER")
        field = _require_safe_field(item["field"], field_types)
        op = item["op"]
        if not isinstance(op, str) or op not in FILTER_OPERATORS:
            raise OneCDiagnosticError("INVALID_FILTER_OPERATOR")
        literal = _odata_literal(field_types[field], item["value"])
        clauses.append(f"{field} {op} {literal}")
        selective = selective or _is_selective_filter(field)
    if not selective:
        raise OneCDiagnosticError("SELECTIVE_FILTER_REQUIRED")
    organization_clause = " or ".join(
        f"Организация_Key eq guid'{guid}'" for guid in organization_guids
    )
    return " and ".join(f"({clause})" for clause in clauses) + f" and ({organization_clause})"


def _json_safe(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def read_entity_rows(
    config: ODataConfig,
    entity_set: str,
    *,
    fields: Iterable[str],
    filters: Iterable[Mapping[str, object]],
    top: int = 20,
    opener=None,
    metadata_raw: bytes | None = None,
):
    """Read a small organization-scoped slice of one published OData entity.

    The caller supplies structured fields and predicates, never a URL or raw
    OData expression.  Entities without ``Организация_Key`` are intentionally
    denied in this first foundation because their organization scope cannot be
    proven server-side yet.
    """
    config = _validated_config(config)
    if not isinstance(entity_set, str) or not IDENTIFIER_RE.fullmatch(entity_set) or "." in entity_set:
        raise OneCDiagnosticError("INVALID_ENTITY_SET")
    if not _is_readable_entity(entity_set):
        raise OneCDiagnosticError("ENTITY_SET_NOT_ALLOWED")
    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= MAX_READ_ROWS:
        raise OneCDiagnosticError("ROW_LIMIT_OUT_OF_RANGE")

    raw = metadata_raw if metadata_raw is not None else fetch_metadata(config, opener=opener)
    schema = parse_metadata(raw).get(entity_set)
    if schema is None:
        raise OneCDiagnosticError("ENTITY_SET_NOT_PUBLISHED")
    field_types = schema.field_types
    if field_types.get("Организация_Key") != "Edm.Guid":
        raise OneCDiagnosticError("ORGANIZATION_SCOPE_UNAVAILABLE")

    requested_fields = list(dict.fromkeys(fields))
    if not requested_fields or len(requested_fields) > MAX_READ_FIELDS:
        raise OneCDiagnosticError("FIELD_COUNT_OUT_OF_RANGE")
    requested_fields = [_require_safe_field(name, field_types) for name in requested_fields]
    expression = _build_filter_expression(filters, field_types, config.organization_guids)

    transport_fields = list(requested_fields)
    if "Организация_Key" not in transport_fields:
        transport_fields.append("Организация_Key")
    query = "&".join((
        f"$select={quote(','.join(transport_fields))}",
        f"$filter={quote(expression)}",
        f"$top={top}",
    ))
    initial_url = f"{config.base_url}{quote(entity_set, safe='')}?{query}"

    rows = []
    try:
        for raw_rows, _page_count in read_odata_pages(config, initial_url, opener=opener):
            for raw_row in raw_rows:
                if not isinstance(raw_row, dict):
                    raise OneCDiagnosticError("INVALID_ODATA_ROW")
                try:
                    organization = str(UUID(str(raw_row.get("Организация_Key")))).lower()
                except (TypeError, ValueError, AttributeError) as exc:
                    raise OneCDiagnosticError("INVALID_ORGANIZATION_SCOPE") from exc
                if organization not in config.organization_guids:
                    raise OneCDiagnosticError("ORGANIZATION_SCOPE_VIOLATION")
                if len(rows) >= top:
                    raise OneCDiagnosticError("ODATA_ROW_LIMIT_VIOLATION")
                rows.append({
                    field: _json_safe(raw_row.get(field))
                    for field in requested_fields
                })
    except OneCDiagnosticError:
        raise
    except ODataPreviewError as exc:
        raise OneCDiagnosticError("ODATA_QUERY_FAILED") from exc

    return {
        "kind": "onec_diagnostic_rows",
        "entity_set": entity_set,
        "row_count": len(rows),
        "rows": rows,
        "limit": top,
        "organization_scope_enforced": True,
        "source": "live_1c_odata",
    }


__all__ = [
    "EntitySchema",
    "OneCDiagnosticError",
    "config_from_settings",
    "describe_metadata",
    "fetch_metadata",
    "get_entity_schema",
    "parse_metadata",
    "read_entity_rows",
]
