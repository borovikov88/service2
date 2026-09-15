"""Bounded, read-only 1C OData diagnostics for future MCP tools.

This module deliberately does not expose an HTTP/MCP endpoint. It provides a
small server-side contract that can inspect published OData metadata and read a
strictly bounded set of rows. The external diagnostic MCP can be layered on
this contract after its own authorization model is reviewed.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json
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
    _safe_next_url,
    validate_config,
)
from pool_service.models import OrganizationAccess


MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_ENTITY_SETS = 10000
MAX_PROPERTIES_PER_ENTITY = 1000
MAX_LIST_ENTITIES = 200
MAX_READ_FIELDS = 40
MAX_READ_FILTERS = 8
MAX_READ_ROWS = 50
MAX_READ_PAGES = 5
MAX_ROW_PAGE_BYTES = 2 * 1024 * 1024
MAX_NUMERIC_INPUT_CHARS = 80
MAX_NUMERIC_EXPONENT_ABS = 100
MAX_NUMERIC_RENDERED_CHARS = 160

DIAGNOSTIC_ACCESS_ROLES = frozenset({"owner", "accountant"})

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

# Generic row reads are deliberately scalar-only. Complex/custom EDM types and
# Collection(...) values can contain nested secrets or unbounded tabular data,
# so the generic reader rejects them before constructing an OData request.
READABLE_PRIMITIVE_EDM_TYPES = frozenset({
    "Edm.String",
    "Edm.Guid",
    "Edm.Boolean",
    "Edm.Byte",
    "Edm.SByte",
    "Edm.Int16",
    "Edm.Int32",
    "Edm.Int64",
    "Edm.Decimal",
    "Edm.Double",
    "Edm.Single",
    "Edm.DateTime",
    "Edm.DateTimeOffset",
    "Edm.Date",
    "Edm.Time",
})

CREDENTIAL_FIELD_RE = re.compile(
    r"password|парол|secret|секрет|token|токен|credential|"
    r"api[_ ]?key|access[_ ]?key|private[_ ]?key|session[_ ]?key|"
    r"auth(?:entication|orization)?[_ ]?(?:key|secret|token)",
    re.IGNORECASE,
)
DIRECT_PERSONAL_IDENTIFIER_FIELD_RE = re.compile(
    r"passport|паспорт|snils|снилс|social[_ ]?security[_ ]?number|"
    r"страхов(?:ой|ого)?[_ ]?(?:номер|номер.*лицев)|номер.*страхов.*свид",
    re.IGNORECASE,
)
NORMALIZED_DIRECT_IDENTIFIER_PARTS = frozenset({
    "passport",
    "паспорт",
    "snils",
    "снилс",
    "ssn",
    "socialsecuritynumber",
})
CRYPTOGRAPHIC_SECRET_FIELD_RE = re.compile(
    r"private[_ ]?key|закрыт.*ключ|certificate.*(?:private|key|sign)|"
    r"сертификат.*(?:эп|подпис|ключ)|электронн.*подпис|ключ.*(?:эп|подпис)",
    re.IGNORECASE,
)
PERSONNEL_ENTITY_RE = re.compile(
    r"employee|personnel|payroll|salary|wage|person|"
    r"сотрудник|физическ.*лиц|физлиц|кадр|персонал|зарплат|начисл|удерж|"
    r"ндфл|табел|больнич|отпуск|страхов.*взнос|расч[её]т.*зарплат",
    re.IGNORECASE,
)

# These names are private only in personnel/payroll entities. Business bank
# accounts and organization tax identifiers elsewhere in 1C remain available.
PERSONNEL_PRIVATE_FIELD_PARTS = frozenset({
    "address",
    "homeaddress",
    "phone",
    "mobile",
    "email",
    "birthdate",
    "dateofbirth",
    "iban",
    "bankaccount",
    "bankdetails",
    "accountnumber",
    "cardnumber",
    "paymentcard",
    "адрес",
    "почта",
    "электроннаяпочта",
    "телефон",
    "мобильныйтелефон",
    "датарождения",
    "банковскиереквизиты",
    "банковскиеданные",
    "банковскийсчет",
    "банковскийсчёт",
    "расчетныйсчет",
    "расчётныйсчёт",
    "лицевойсчет",
    "лицевойсчёт",
    "номерсчета",
    "номерсчёта",
    "номеркарты",
    "банковскаякарта",
})
PERSONNEL_PRIVATE_FIELD_EXACT = frozenset({
    "инн",
    "taxid",
    "taxpayerid",
    "бик",
})


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


def _target_organization_id() -> int | None:
    """Return configured Service2 organization id, failing closed on bad config."""
    raw = getattr(settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", None)
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def can_access_diagnostic_mcp(user, organization) -> bool:
    """Allow only active owner/accountant of the configured 1C target organization."""
    if (
        not user
        or not getattr(user, "is_authenticated", False)
        or not getattr(user, "is_active", False)
        or not organization
    ):
        return False
    target_id = _target_organization_id()
    if target_id is None or getattr(organization, "pk", None) != target_id:
        return False
    return OrganizationAccess.objects.filter(
        user=user,
        organization_id=target_id,
        role__in=DIAGNOSTIC_ACCESS_ROLES,
    ).exists()


def config_from_settings() -> ODataConfig:
    """Use the existing 1C connection without creating another credential path."""
    return ODataConfig(
        base_url=settings.ONEC_ODATA_BASE_URL,
        username=settings.ONEC_ODATA_USERNAME,
        password=settings.ONEC_ODATA_PASSWORD,
        organization_guids=tuple(settings.ONEC_ODATA_ORGANIZATION_GUIDS),
        timeout_seconds=settings.ONEC_ODATA_TIMEOUT_SECONDS,
        max_pages=min(int(settings.ONEC_ODATA_MAX_PAGES), MAX_READ_PAGES),
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


def _authorization(config: ODataConfig) -> str:
    if not config.username:
        return ""
    token = base64.b64encode(
        f"{config.username}:{config.password}".encode("utf-8")
    ).decode("ascii")
    return f"Basic {token}"


def fetch_metadata(config: ODataConfig, *, opener=None) -> bytes:
    """Fetch `$metadata` over a bounded GET-only HTTPS request."""
    config = _validated_config(config)
    request = Request(
        config.base_url + "$metadata",
        headers={"Accept": "application/xml"},
        method="GET",
    )
    authorization = _authorization(config)
    if authorization:
        request.add_header("Authorization", authorization)
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


def _is_readable_primitive(declared_type: str) -> bool:
    return declared_type in READABLE_PRIMITIVE_EDM_TYPES


def _normalized_field_name(name: str) -> str:
    return "".join(character for character in name.casefold() if character.isalnum())


def _has_normalized_direct_identifier(name: str) -> bool:
    normalized = _normalized_field_name(name)
    return any(part in normalized for part in NORMALIZED_DIRECT_IDENTIFIER_PARTS)


def _personnel_private_field(name: str) -> bool:
    normalized = _normalized_field_name(name)
    if normalized in PERSONNEL_PRIVATE_FIELD_EXACT:
        return True
    return any(part in normalized for part in PERSONNEL_PRIVATE_FIELD_PARTS)


def _field_is_sensitive(name: str, *, entity_set: str = "") -> bool:
    """Deny secrets/private identifiers while allowing payroll business facts."""
    if CREDENTIAL_FIELD_RE.search(name):
        return True
    if DIRECT_PERSONAL_IDENTIFIER_FIELD_RE.search(name) or _has_normalized_direct_identifier(name):
        return True
    if CRYPTOGRAPHIC_SECRET_FIELD_RE.search(name):
        return True
    if entity_set and PERSONNEL_ENTITY_RE.search(entity_set):
        return _personnel_private_field(name)
    return False


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
        and (
            not needle
            or needle in schema.name.casefold()
            or any(needle in field.casefold() for field, _ in schema.properties)
        )
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
                "row_access_allowed": True,
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
        "row_access_allowed": True,
        "fields": [
            {
                "name": name,
                "type": declared,
                "sensitive": _field_is_sensitive(name, entity_set=schema.name),
                "readable_scalar": _is_readable_primitive(declared),
            }
            for name, declared in schema.properties
        ],
    }


def _require_safe_field(
    name: str,
    field_types: Mapping[str, str],
    *,
    entity_set: str,
) -> str:
    if not isinstance(name, str) or not IDENTIFIER_RE.fullmatch(name) or "." in name:
        raise OneCDiagnosticError("INVALID_FIELD")
    if name not in field_types:
        raise OneCDiagnosticError("FIELD_NOT_PUBLISHED")
    if not _is_readable_primitive(field_types[name]):
        raise OneCDiagnosticError("NON_PRIMITIVE_FIELD_DENIED")
    if _field_is_sensitive(name, entity_set=entity_set):
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


def _numeric_literal(value) -> str:
    if isinstance(value, bool):
        raise OneCDiagnosticError("INVALID_FILTER_VALUE")
    text = str(value).strip()
    if not text or len(text) > MAX_NUMERIC_INPUT_CHARS:
        raise OneCDiagnosticError("INVALID_FILTER_VALUE")
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OneCDiagnosticError("INVALID_FILTER_VALUE") from exc
    if not parsed.is_finite():
        raise OneCDiagnosticError("INVALID_FILTER_VALUE")
    decimal_tuple = parsed.as_tuple()
    if (
        len(decimal_tuple.digits) > MAX_NUMERIC_INPUT_CHARS
        or abs(decimal_tuple.exponent) > MAX_NUMERIC_EXPONENT_ABS
        or (parsed and abs(parsed.adjusted()) > MAX_NUMERIC_EXPONENT_ABS)
    ):
        raise OneCDiagnosticError("INVALID_FILTER_VALUE")
    rendered = format(parsed, "f")
    if len(rendered) > MAX_NUMERIC_RENDERED_CHARS:
        raise OneCDiagnosticError("INVALID_FILTER_VALUE")
    return rendered


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
    if declared_type in {
        "Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte", "Edm.SByte",
    }:
        if isinstance(value, bool):
            raise OneCDiagnosticError("INVALID_FILTER_VALUE")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise OneCDiagnosticError("INVALID_FILTER_VALUE") from exc
        if str(parsed) != str(value).strip():
            raise OneCDiagnosticError("INVALID_FILTER_VALUE")
        return str(parsed)
    if declared_type in {"Edm.Decimal", "Edm.Double", "Edm.Single"}:
        return _numeric_literal(value)
    raise OneCDiagnosticError("FILTER_TYPE_NOT_SUPPORTED")


def _is_selective_filter(field: str) -> bool:
    return field in {"Ref_Key", "Number", "Date", "Period"} or field.endswith("_Key")


def _build_filter_expression(
    filters: Iterable[Mapping[str, object]],
    field_types: Mapping[str, str],
    organization_guids: tuple[str, ...],
    *,
    entity_set: str,
) -> str:
    if isinstance(filters, (str, bytes)):
        raise OneCDiagnosticError("INVALID_FILTER")
    try:
        filters = list(filters)
    except TypeError as exc:
        raise OneCDiagnosticError("INVALID_FILTER") from exc
    if not filters or len(filters) > MAX_READ_FILTERS:
        raise OneCDiagnosticError("FILTER_COUNT_OUT_OF_RANGE")
    clauses = []
    selective = False
    for item in filters:
        if not isinstance(item, Mapping) or set(item) != {"field", "op", "value"}:
            raise OneCDiagnosticError("INVALID_FILTER")
        field = _require_safe_field(item["field"], field_types, entity_set=entity_set)
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
    return (
        " and ".join(f"({clause})" for clause in clauses)
        + f" and ({organization_clause})"
    )


def _payload_page(raw: bytes):
    try:
        payload = json.loads(raw.decode("utf-8-sig"), parse_float=Decimal)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OneCDiagnosticError("ODATA_INVALID_JSON") from exc
    if not isinstance(payload, dict):
        raise OneCDiagnosticError("ODATA_INVALID_PAYLOAD")
    if "d" in payload:
        legacy = payload.get("d")
        if not isinstance(legacy, dict) or not isinstance(legacy.get("results"), list):
            raise OneCDiagnosticError("ODATA_INVALID_PAYLOAD")
        next_link = legacy.get("__next")
        if next_link is not None and not isinstance(next_link, str):
            raise OneCDiagnosticError("ODATA_INVALID_PAYLOAD")
        return legacy["results"], next_link
    rows = payload.get("value")
    if not isinstance(rows, list):
        raise OneCDiagnosticError("ODATA_INVALID_PAYLOAD")
    next_link = payload.get("@odata.nextLink") or payload.get("odata.nextLink")
    if next_link is not None and not isinstance(next_link, str):
        raise OneCDiagnosticError("ODATA_INVALID_PAYLOAD")
    return rows, next_link


def _bounded_odata_pages(config: ODataConfig, initial_url: str, *, opener=None):
    """Read GET-only OData pages with same-origin and per-page byte limits."""
    config = _validated_config(config)
    try:
        current_url = _safe_next_url(config.base_url, config.base_url, initial_url)
    except ODataPreviewError as exc:
        raise OneCDiagnosticError("ODATA_UNSAFE_URL") from exc

    client = opener or build_opener(NoRedirectHandler())
    authorization = _authorization(config)
    seen_urls: set[str] = set()
    page_limit = min(config.max_pages, MAX_READ_PAGES)

    for page_count in range(1, page_limit + 1):
        if current_url in seen_urls:
            raise OneCDiagnosticError("ODATA_PAGINATION_LOOP")
        seen_urls.add(current_url)
        request = Request(current_url, headers={"Accept": "application/json"}, method="GET")
        if authorization:
            request.add_header("Authorization", authorization)
        try:
            with client.open(request, timeout=config.timeout_seconds) as response:
                if getattr(response, "status", 200) != 200:
                    raise OneCDiagnosticError("ODATA_HTTP_NOT_200")
                raw = response.read(MAX_ROW_PAGE_BYTES + 1)
        except OneCDiagnosticError:
            raise
        except HTTPError as exc:
            raise OneCDiagnosticError("ODATA_HTTP_ERROR") from exc
        except URLError as exc:
            raise OneCDiagnosticError("ODATA_REQUEST_FAILED") from exc

        if len(raw) > MAX_ROW_PAGE_BYTES:
            raise OneCDiagnosticError("ODATA_PAGE_SIZE_LIMIT")
        rows, next_link = _payload_page(raw)
        yield rows, page_count
        if not next_link:
            return
        try:
            current_url = _safe_next_url(config.base_url, current_url, next_link)
        except ODataPreviewError as exc:
            raise OneCDiagnosticError("ODATA_UNSAFE_NEXT_LINK") from exc

    raise OneCDiagnosticError("ODATA_PAGINATION_LIMIT")


def _json_safe_scalar(value):
    """Return only JSON scalar values; nested payloads are fail-closed."""
    if isinstance(value, (dict, list)):
        raise OneCDiagnosticError("NON_PRIMITIVE_FIELD_DENIED")
    if isinstance(value, Decimal):
        return str(value)
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
    """Read a bounded organization-scoped slice of one published OData entity."""
    config = _validated_config(config)
    if (
        not isinstance(entity_set, str)
        or not IDENTIFIER_RE.fullmatch(entity_set)
        or "." in entity_set
    ):
        raise OneCDiagnosticError("INVALID_ENTITY_SET")
    if not _is_readable_entity(entity_set):
        raise OneCDiagnosticError("ENTITY_SET_NOT_ALLOWED")
    row_limit = min(config.max_rows, MAX_READ_ROWS)
    if isinstance(top, bool) or not isinstance(top, int) or not 1 <= top <= row_limit:
        raise OneCDiagnosticError("ROW_LIMIT_OUT_OF_RANGE")

    raw = metadata_raw if metadata_raw is not None else fetch_metadata(config, opener=opener)
    schema = parse_metadata(raw).get(entity_set)
    if schema is None:
        raise OneCDiagnosticError("ENTITY_SET_NOT_PUBLISHED")
    field_types = schema.field_types
    if field_types.get("Организация_Key") != "Edm.Guid":
        raise OneCDiagnosticError("ORGANIZATION_SCOPE_UNAVAILABLE")

    if isinstance(fields, (str, bytes)):
        raise OneCDiagnosticError("INVALID_FIELD")
    try:
        requested_fields = list(dict.fromkeys(fields))
    except (TypeError, AttributeError) as exc:
        raise OneCDiagnosticError("INVALID_FIELD") from exc
    if not requested_fields or len(requested_fields) > MAX_READ_FIELDS:
        raise OneCDiagnosticError("FIELD_COUNT_OUT_OF_RANGE")
    requested_fields = [
        _require_safe_field(name, field_types, entity_set=entity_set)
        for name in requested_fields
    ]
    expression = _build_filter_expression(
        filters,
        field_types,
        config.organization_guids,
        entity_set=entity_set,
    )

    transport_fields = list(requested_fields)
    if "Организация_Key" not in transport_fields:
        transport_fields.append("Организация_Key")
    query = "&".join((
        f"$select={quote(','.join(transport_fields))}",
        f"$filter={quote(expression)}",
        f"$top={top}",
    ))
    initial_url = f"{config.base_url}{quote(entity_set, safe='')}?{query}"

    allowed_organizations = {guid.lower() for guid in config.organization_guids}
    rows = []
    for raw_rows, _page_count in _bounded_odata_pages(
        config, initial_url, opener=opener
    ):
        for raw_row in raw_rows:
            if not isinstance(raw_row, dict):
                raise OneCDiagnosticError("INVALID_ODATA_ROW")
            try:
                organization = str(UUID(str(raw_row.get("Организация_Key")))).lower()
            except (TypeError, ValueError, AttributeError) as exc:
                raise OneCDiagnosticError("INVALID_ORGANIZATION_SCOPE") from exc
            if organization not in allowed_organizations:
                raise OneCDiagnosticError("ORGANIZATION_SCOPE_VIOLATION")
            if len(rows) >= top:
                raise OneCDiagnosticError("ODATA_ROW_LIMIT_VIOLATION")
            rows.append({
                field: _json_safe_scalar(raw_row.get(field))
                for field in requested_fields
            })

    return {
        "kind": "onec_diagnostic_rows",
        "entity_set": entity_set,
        "row_count": len(rows),
        "rows": rows,
        "limit": top,
        "organization_scope_enforced": True,
        "sensitive_personal_security_fields_denied": True,
        "non_primitive_fields_denied": True,
        "page_byte_limit": MAX_ROW_PAGE_BYTES,
        "source": "live_1c_odata",
    }


__all__ = [
    "DIAGNOSTIC_ACCESS_ROLES",
    "EntitySchema",
    "MAX_ROW_PAGE_BYTES",
    "OneCDiagnosticError",
    "READABLE_PRIMITIVE_EDM_TYPES",
    "can_access_diagnostic_mcp",
    "config_from_settings",
    "describe_metadata",
    "fetch_metadata",
    "get_entity_schema",
    "parse_metadata",
    "read_entity_rows",
]
