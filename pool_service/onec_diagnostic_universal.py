"""Universal metadata-driven read-only 1C query layer.

This module deliberately reuses the existing Diagnostic transport, metadata,
field-policy and OAuth/audit surface.  It only widens the read composition
surface; it never adds a write/import path or accepts raw OData fragments.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time
from typing import Iterable, Mapping
from urllib.parse import quote
from uuid import UUID

from django.conf import settings

from pool_service import onec_diagnostic as base


MAX_QUERY_ROWS = 500
MAX_QUERY_PAGES = 100
MAX_QUERY_ORDER_FIELDS = 3
MAX_IN_VALUES = 50
QUERY_FILTER_OPERATORS = frozenset({
    "eq", "ne", "gt", "ge", "lt", "le", "contains", "startswith", "in"
})



def _date_literal(value) -> str:
    if isinstance(value, datetime):
        raise base.OneCDiagnosticError("INVALID_FILTER_VALUE")
    if isinstance(value, date):
        parsed = value
    elif isinstance(value, str) and len(value) <= 10:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise base.OneCDiagnosticError("INVALID_FILTER_VALUE") from exc
    else:
        raise base.OneCDiagnosticError("INVALID_FILTER_VALUE")
    return f"date'{parsed.isoformat()}'"


def _time_literal(value) -> str:
    if isinstance(value, time):
        parsed = value
    elif isinstance(value, str) and len(value) <= 16:
        try:
            parsed = time.fromisoformat(value)
        except ValueError as exc:
            raise base.OneCDiagnosticError("INVALID_FILTER_VALUE") from exc
    else:
        raise base.OneCDiagnosticError("INVALID_FILTER_VALUE")
    if parsed.tzinfo is not None:
        raise base.OneCDiagnosticError("INVALID_FILTER_VALUE")
    return f"time'{parsed.isoformat(timespec='seconds')}'"


def _odata_literal(declared_type: str, value) -> str:
    if declared_type == "Edm.Date":
        return _date_literal(value)
    if declared_type == "Edm.Time":
        return _time_literal(value)
    return base._odata_literal(declared_type, value)


def _normalize_filters(
    filters: Iterable[Mapping[str, object]],
    field_types: Mapping[str, str],
    *,
    entity_set: str,
):
    if isinstance(filters, (str, bytes)):
        raise base.OneCDiagnosticError("INVALID_FILTER")
    try:
        items = list(filters)
    except TypeError as exc:
        raise base.OneCDiagnosticError("INVALID_FILTER") from exc
    if not items or len(items) > base.MAX_READ_FILTERS:
        raise base.OneCDiagnosticError("FILTER_COUNT_OUT_OF_RANGE")

    clauses: list[str] = []
    key_anchor = False
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"field", "op", "value"}:
            raise base.OneCDiagnosticError("INVALID_FILTER")
        field = base._require_safe_field(
            item["field"], field_types, entity_set=entity_set
        )
        op = item["op"]
        if not isinstance(op, str) or op not in QUERY_FILTER_OPERATORS:
            raise base.OneCDiagnosticError("INVALID_FILTER_OPERATOR")
        declared = field_types[field]
        value = item["value"]

        if op in {"contains", "startswith"}:
            if declared != "Edm.String":
                raise base.OneCDiagnosticError("FILTER_TYPE_NOT_SUPPORTED")
            if not isinstance(value, str) or not value or len(value) > 300:
                raise base.OneCDiagnosticError("INVALID_FILTER_VALUE")
            literal = base._string_literal(value)
            if op == "contains":
                clauses.append(f"substringof({literal},{field}) eq true")
            else:
                clauses.append(f"startswith({field},{literal}) eq true")
            continue

        if op == "in":
            if isinstance(value, (str, bytes)):
                raise base.OneCDiagnosticError("INVALID_FILTER_VALUE")
            try:
                values = list(value)
            except TypeError as exc:
                raise base.OneCDiagnosticError("INVALID_FILTER_VALUE") from exc
            if not 1 <= len(values) <= MAX_IN_VALUES:
                raise base.OneCDiagnosticError("INVALID_FILTER_VALUE")
            literals = [_odata_literal(declared, item_value) for item_value in values]
            clauses.append("(" + " or ".join(
                f"{field} eq {literal}" for literal in literals
            ) + ")")
            if field == "Ref_Key":
                key_anchor = True
            continue

        literal = _odata_literal(declared, value)
        clauses.append(f"{field} {op} {literal}")
        if op == "eq" and field == "Ref_Key":
            key_anchor = True

    return clauses, key_anchor



def _ref_key_anchor_values(filters, field_types, *, entity_set: str) -> set[str]:
    """Return the validated parent-document Ref_Key anchor values, if any."""
    if field_types.get("Ref_Key") != "Edm.Guid":
        return set()
    for item in filters:
        if not isinstance(item, Mapping) or item.get("field") != "Ref_Key":
            continue
        op = item.get("op")
        if op == "eq":
            values = [item.get("value")]
        elif op == "in":
            raw_values = item.get("value")
            if isinstance(raw_values, (str, bytes)):
                return set()
            try:
                values = list(raw_values)
            except TypeError:
                return set()
        else:
            continue
        result = set()
        for value in values:
            try:
                result.add(str(UUID(str(value))).lower())
            except (TypeError, ValueError, AttributeError) as exc:
                raise base.OneCDiagnosticError("INVALID_FILTER_VALUE") from exc
        return result
    return set()


def _document_parent_entity(index, entity_set: str):
    """Find the longest organization-scoped Document_ parent for a tabular part."""
    if not entity_set.startswith("Document_"):
        return None
    candidates = []
    for name, schema in index.items():
        if (
            name == entity_set
            or not name.startswith("Document_")
            or not entity_set.startswith(name + "_")
        ):
            continue
        field_types = schema.field_types
        if (
            field_types.get("Ref_Key") == "Edm.Guid"
            and field_types.get("Организация_Key") == "Edm.Guid"
        ):
            candidates.append(name)
    return max(candidates, key=len) if candidates else None


def _validate_parent_document_scope(
    config,
    index,
    entity_set: str,
    ref_keys: set[str],
    *,
    opener=None,
) -> set[str]:
    """Prove every child Ref_Key belongs to an allowed-organization document."""
    if not ref_keys:
        raise base.OneCDiagnosticError("KEY_ANCHOR_REQUIRED")
    parent_entity = _document_parent_entity(index, entity_set)
    if parent_entity is None:
        raise base.OneCDiagnosticError("ORGANIZATION_SCOPE_UNAVAILABLE")

    allowed_organizations = {
        str(UUID(value)).lower() for value in config.organization_guids
    }
    key_clause = " or ".join(
        f"Ref_Key eq guid'{value}'" for value in sorted(ref_keys)
    )
    organization_clause = " or ".join(
        f"Организация_Key eq guid'{value}'"
        for value in sorted(allowed_organizations)
    )
    expression = f"({key_clause}) and ({organization_clause})"
    parent_url = (
        f"{config.base_url}{quote(parent_entity, safe='')}?"
        f"$select={quote('Ref_Key,Организация_Key')}&"
        f"$filter={quote(expression)}&$top={len(ref_keys) + 1}"
    )

    found: set[str] = set()
    for raw_rows, _page_count in base._bounded_odata_pages(
        config,
        parent_url,
        opener=opener,
        max_pages=min(config.max_pages, MAX_QUERY_PAGES),
    ):
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                raise base.OneCDiagnosticError("INVALID_ODATA_ROW")
            try:
                ref_key = str(UUID(str(raw_row.get("Ref_Key")))).lower()
                organization = str(
                    UUID(str(raw_row.get("Организация_Key")))
                ).lower()
            except (TypeError, ValueError, AttributeError) as exc:
                raise base.OneCDiagnosticError(
                    "PARENT_ORGANIZATION_SCOPE_INVALID"
                ) from exc
            if ref_key not in ref_keys or organization not in allowed_organizations:
                raise base.OneCDiagnosticError(
                    "PARENT_ORGANIZATION_SCOPE_VIOLATION"
                )
            if ref_key in found:
                raise base.OneCDiagnosticError(
                    "PARENT_ORGANIZATION_SCOPE_VIOLATION"
                )
            found.add(ref_key)

    if found != ref_keys:
        raise base.OneCDiagnosticError("PARENT_ORGANIZATION_SCOPE_VIOLATION")
    return found


def _normalize_order_by(
    order_by,
    field_types: Mapping[str, str],
    *,
    entity_set: str,
) -> list[str]:
    if order_by is None:
        return []
    if isinstance(order_by, (str, bytes)):
        raise base.OneCDiagnosticError("INVALID_ORDER_BY")
    try:
        items = list(order_by)
    except TypeError as exc:
        raise base.OneCDiagnosticError("INVALID_ORDER_BY") from exc
    if len(items) > MAX_QUERY_ORDER_FIELDS:
        raise base.OneCDiagnosticError("ORDER_BY_COUNT_OUT_OF_RANGE")
    result = []
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"field", "direction"}:
            raise base.OneCDiagnosticError("INVALID_ORDER_BY")
        field = base._require_safe_field(
            item["field"], field_types, entity_set=entity_set
        )
        direction = item["direction"]
        if direction not in {"asc", "desc"}:
            raise base.OneCDiagnosticError("INVALID_ORDER_DIRECTION")
        result.append(f"{field} {direction}")
    return result


def config_from_settings():
    """Use the Diagnostic connection while preserving the universal page budget."""
    legacy = base.config_from_settings()
    return replace(
        legacy,
        max_pages=min(int(settings.ONEC_ODATA_MAX_PAGES), MAX_QUERY_PAGES),
    )


def query_1c_rows(
    config,
    entity_set: str,
    *,
    fields: Iterable[str],
    filters: Iterable[Mapping[str, object]],
    limit: int = 100,
    include_deleted: bool = False,
    include_inactive: bool = False,
    order_by=None,
    opener=None,
    metadata_raw: bytes | None = None,
):
    """Read policy-approved 1C rows through structured metadata-driven queries."""
    config = base._validated_config(config)
    if (
        not isinstance(entity_set, str)
        or not base.IDENTIFIER_RE.fullmatch(entity_set)
        or "." in entity_set
    ):
        raise base.OneCDiagnosticError("INVALID_ENTITY_SET")
    if not base._is_readable_entity(entity_set):
        raise base.OneCDiagnosticError("ENTITY_SET_NOT_ALLOWED")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_QUERY_ROWS:
        raise base.OneCDiagnosticError("ROW_LIMIT_OUT_OF_RANGE")
    if type(include_deleted) is not bool or type(include_inactive) is not bool:
        raise base.OneCDiagnosticError("INVALID_INCLUDE_FLAG")

    raw = metadata_raw if metadata_raw is not None else base.fetch_metadata(config, opener=opener)
    index = base.parse_metadata(raw)
    schema = index.get(entity_set)
    if schema is None:
        raise base.OneCDiagnosticError("ENTITY_SET_NOT_PUBLISHED")
    field_types = schema.field_types

    if isinstance(fields, (str, bytes)):
        raise base.OneCDiagnosticError("INVALID_FIELD")
    try:
        requested_fields = list(dict.fromkeys(fields))
    except (TypeError, AttributeError) as exc:
        raise base.OneCDiagnosticError("INVALID_FIELD") from exc
    if not requested_fields or len(requested_fields) > base.MAX_READ_FIELDS:
        raise base.OneCDiagnosticError("FIELD_COUNT_OUT_OF_RANGE")
    requested_fields = [
        base._require_safe_field(name, field_types, entity_set=entity_set)
        for name in requested_fields
    ]

    if isinstance(filters, (str, bytes)):
        raise base.OneCDiagnosticError("INVALID_FILTER")
    try:
        filter_items = list(filters)
    except TypeError as exc:
        raise base.OneCDiagnosticError("INVALID_FILTER") from exc

    clauses, _key_anchor = _normalize_filters(
        filter_items, field_types, entity_set=entity_set
    )
    ordering = _normalize_order_by(
        order_by, field_types, entity_set=entity_set
    )

    organization_present = "Организация_Key" in field_types
    if organization_present and field_types["Организация_Key"] != "Edm.Guid":
        raise base.OneCDiagnosticError("ORGANIZATION_SCOPE_INVALID")
    organization_scoped = organization_present
    parent_scoped = False
    verified_parent_refs: set[str] = set()
    if not organization_scoped and not entity_set.startswith("Catalog_"):
        ref_keys = _ref_key_anchor_values(
            filter_items, field_types, entity_set=entity_set
        )
        verified_parent_refs = _validate_parent_document_scope(
            config,
            index,
            entity_set,
            ref_keys,
            opener=opener,
        )
        parent_scoped = True

    transport_fields = list(requested_fields)
    allowed_organizations: set[str] = set()
    if organization_scoped:
        allowed_organizations = {
            str(UUID(value)).lower() for value in config.organization_guids
        }
        clauses.append("(" + " or ".join(
            f"Организация_Key eq guid'{value}'"
            for value in sorted(allowed_organizations)
        ) + ")")
        if "Организация_Key" not in transport_fields:
            transport_fields.append("Организация_Key")
    if parent_scoped and "Ref_Key" not in transport_fields:
        transport_fields.append("Ref_Key")

    deleted_rows_excluded = (
        field_types.get("DeletionMark") == "Edm.Boolean" and not include_deleted
    )
    if deleted_rows_excluded:
        clauses.append("DeletionMark eq false")
        if "DeletionMark" not in transport_fields:
            transport_fields.append("DeletionMark")

    inactive_rows_excluded = (
        field_types.get("Active") == "Edm.Boolean" and not include_inactive
    )
    if inactive_rows_excluded:
        clauses.append("Active eq true")
        if "Active" not in transport_fields:
            transport_fields.append("Active")

    query_parts = [
        f"$select={quote(','.join(transport_fields))}",
        f"$filter={quote(' and '.join(f'({clause})' for clause in clauses))}",
        f"$top={limit + 1}",
    ]
    if ordering:
        query_parts.append(f"$orderby={quote(','.join(ordering))}")
    initial_url = (
        f"{config.base_url}{quote(entity_set, safe='')}?" + "&".join(query_parts)
    )

    rows = []
    truncated = False
    pagination_limited = False
    try:
        for raw_rows, _page_count in base._bounded_odata_pages(
            config,
            initial_url,
            opener=opener,
            max_pages=min(config.max_pages, MAX_QUERY_PAGES),
        ):
            for raw_row in raw_rows:
                if not isinstance(raw_row, dict):
                    raise base.OneCDiagnosticError("INVALID_ODATA_ROW")
                if organization_scoped:
                    try:
                        organization = str(UUID(str(raw_row.get("Организация_Key")))).lower()
                    except (TypeError, ValueError, AttributeError) as exc:
                        raise base.OneCDiagnosticError("INVALID_ORGANIZATION_SCOPE") from exc
                    if organization not in allowed_organizations:
                        raise base.OneCDiagnosticError("ORGANIZATION_SCOPE_VIOLATION")
                if parent_scoped:
                    try:
                        child_ref = str(UUID(str(raw_row.get("Ref_Key")))).lower()
                    except (TypeError, ValueError, AttributeError) as exc:
                        raise base.OneCDiagnosticError(
                            "PARENT_ORGANIZATION_SCOPE_INVALID"
                        ) from exc
                    if child_ref not in verified_parent_refs:
                        raise base.OneCDiagnosticError(
                            "PARENT_ORGANIZATION_SCOPE_VIOLATION"
                        )
                if deleted_rows_excluded and raw_row.get("DeletionMark") is not False:
                    raise base.OneCDiagnosticError("DELETION_SCOPE_VIOLATION")
                if inactive_rows_excluded and raw_row.get("Active") is not True:
                    raise base.OneCDiagnosticError("INACTIVE_SCOPE_VIOLATION")
                if len(rows) >= limit:
                    truncated = True
                    break
                rows.append({
                    field: base._json_safe_scalar(raw_row.get(field))
                    for field in requested_fields
                })
            if truncated:
                break
    except base.OneCDiagnosticError as exc:
        if exc.code == "ODATA_PAGINATION_LIMIT":
            pagination_limited = True
        else:
            raise

    truncated = truncated or pagination_limited
    return {
        "kind": "onec_query_rows",
        "entity_set": entity_set,
        "row_count": len(rows),
        "rows": rows,
        "limit": limit,
        "complete": not truncated,
        "truncated": truncated,
        "organization_scope_enforced": organization_scoped or parent_scoped,
        "deleted_rows_excluded": deleted_rows_excluded,
        "inactive_rows_excluded": inactive_rows_excluded,
        "sensitive_personal_security_fields_denied": True,
        "non_primitive_fields_denied": True,
        "page_byte_limit": base.MAX_ROW_PAGE_BYTES,
        "source": "live_1c_odata",
    }




__all__ = [
    "MAX_IN_VALUES",
    "MAX_QUERY_ORDER_FIELDS",
    "MAX_QUERY_ROWS",
    "QUERY_FILTER_OPERATORS",
    "config_from_settings",
    "query_1c_rows",
]
