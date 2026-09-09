"""Draft and confirmation services for read-only 1C OData profit snapshots."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from urllib.parse import quote
from urllib.request import build_opener
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from pool_service.models import (
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCReportPeriodState,
    Organization,
    onec_monthly_profit_source_identity,
)
from .odata_profit import (
    NoRedirectHandler,
    ODataConfig,
    ODataPreviewError,
    PROFIT_DOCUMENT_TYPES,
    PROFIT_RECORDER_TYPES,
    ProfitRow,
    ZERO_GUID,
    normalize_document_type,
    normalize_guid,
    parse_month,
    read_odata_pages,
    read_profit_rows,
    validate_config,
)
from .services import (
    ERROR_MESSAGE_MAX_LENGTH,
    _activate_period_states,
    _audit,
    _bulk_create_monthly_rows,
    _save_confirmed_batch,
    calculate_profitability,
)
from .validators import delete_private_batch_file


SNAPSHOT_SCHEMA = "onec_odata_profit_draft_v2"
PARSER_VERSION = "odata-2"
REFERENCE_BATCH_SIZE = 40
MAX_DRAFT_MONTHS = 12
MONEY_QUANTUM = Decimal("0.01")
QUANTITY_QUANTUM = Decimal("0.000001")
CATALOGS = {
    "nomenclature": (
        "Catalog_Номенклатура",
        ("Ref_Key", "Description", "DeletionMark", "Артикул", "ТипНоменклатуры"),
    ),
    "customer": (
        "Catalog_Контрагенты",
        ("Ref_Key", "Description", "DeletionMark"),
    ),
    "responsible": (
        "Catalog_Сотрудники",
        ("Ref_Key", "Description", "DeletionMark"),
    ),
}
DOCUMENTS = {
    "Document_РасходнаяНакладная": {
        "label": "Расходная накладная",
        "fields": ("Ref_Key", "Number", "Date", "Заказ", "Заказ_Type"),
    },
    "Document_ОтчетОРозничныхПродажах": {
        "label": "Отчёт о розничных продажах",
        "fields": ("Ref_Key", "Number", "Date"),
    },
    "Document_ЧекККМ": {
        "label": "Чек ККМ",
        "fields": ("Ref_Key", "Number", "Date"),
    },
    "Document_ЗаказПокупателя": {
        "label": "Заказ покупателя",
        "fields": ("Ref_Key", "Number", "Date"),
    },
}
RETAIL_REPORT_TYPE = "Document_ОтчетОРозничныхПродажах"
RETAIL_CHECK_TYPE = "Document_ЧекККМ"
ORDER_TYPE = "Document_ЗаказПокупателя"


class ODataDraftError(ValidationError):
    def __init__(self, message, *, batch=None):
        self.batch = batch
        super().__init__(message)


def config_from_settings() -> ODataConfig:
    return ODataConfig(
        base_url=settings.ONEC_ODATA_BASE_URL,
        username=settings.ONEC_ODATA_USERNAME,
        password=settings.ONEC_ODATA_PASSWORD,
        organization_guids=tuple(settings.ONEC_ODATA_ORGANIZATION_GUIDS),
        timeout_seconds=settings.ONEC_ODATA_TIMEOUT_SECONDS,
        max_pages=settings.ONEC_ODATA_MAX_PAGES,
        max_rows=settings.ONEC_ODATA_MAX_ROWS,
    )


def is_odata_target_organization(organization) -> bool:
    raw_target = str(
        getattr(settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", "") or ""
    ).strip()
    try:
        target_id = int(raw_target)
    except (TypeError, ValueError):
        return False
    return target_id > 0 and organization.pk == target_id


def _require_odata_target_organization(organization):
    if not is_odata_target_organization(organization):
        raise ODataDraftError("OData import is not configured for this organization")


def _month_scope(start_month, end_month):
    try:
        start = parse_month(start_month)
        end = parse_month(end_month)
    except ODataPreviewError as exc:
        raise ODataDraftError("OData draft period is invalid") from exc
    if end < start:
        raise ODataDraftError("OData draft period is invalid")
    month_count = (end.year - start.year) * 12 + end.month - start.month + 1
    if month_count > MAX_DRAFT_MONTHS:
        raise ODataDraftError("OData draft range cannot exceed 12 months")
    months = []
    current = start
    while current <= end:
        months.append(current)
        current = date(
            current.year + (current.month == 12),
            1 if current.month == 12 else current.month + 1,
            1,
        )
    return months


def _chunks(values, size=REFERENCE_BATCH_SIZE):
    values = list(values)
    for index in range(0, len(values), size):
        yield values[index:index + size]


def _reference_url(config, entity_set, fields, guids):
    expression = " or ".join(f"Ref_Key eq guid'{guid}'" for guid in guids)
    return (
        f"{config.base_url}{quote(entity_set, safe='')}?"
        f"$select={quote(','.join(fields))}&$filter={quote(expression)}"
    )


def _read_reference_map(
    config,
    kind,
    guids,
    *,
    opener,
    page_budget,
    allow_deleted_nomenclature=False,
    allow_deleted_customer=False,
):
    entity_set, fields = CATALOGS[kind]
    expected = set(guids)
    found = {}
    for batch_guids in _chunks(sorted(expected)):
        url = _reference_url(config, entity_set, fields, batch_guids)
        returned = 0
        for raw_rows, _ in read_odata_pages(config, url, opener=opener):
            page_budget["used"] += 1
            if page_budget["used"] > config.max_pages:
                raise ODataPreviewError("1C reference lookups exceeded the page limit")
            returned += len(raw_rows)
            if returned > len(batch_guids):
                raise ODataPreviewError("1C reference lookup returned unexpected rows")
            for raw in raw_rows:
                if not isinstance(raw, dict):
                    raise ODataPreviewError("1C reference row must be an object")
                key = normalize_guid(raw.get("Ref_Key"), field="Ref_Key")
                if key not in batch_guids or key in found:
                    raise ODataPreviewError("1C reference lookup returned an unexpected identity")
                allows_historical_deleted_reference = (
                    raw.get("DeletionMark") is True
                    and (
                        (kind == "nomenclature" and allow_deleted_nomenclature)
                        or (kind == "customer" and allow_deleted_customer)
                    )
                )
                if raw.get("DeletionMark") is not False and not allows_historical_deleted_reference:
                    raise ODataPreviewError("1C reference is deleted or has an invalid deletion mark")
                description = raw.get("Description")
                if not isinstance(description, str) or not description.strip():
                    raise ODataPreviewError("1C reference has no display description")
                try:
                    UUID(description.strip())
                except (ValueError, TypeError, AttributeError):
                    pass
                else:
                    raise ODataPreviewError("1C reference description must not be a GUID")
                description_limit = 300 if kind == "responsible" else 500
                item = {"description": description.strip()[:description_limit]}
                if kind == "nomenclature":
                    article = raw.get("Артикул")
                    if article is not None and not isinstance(article, str):
                        raise ODataPreviewError("1C nomenclature article must be a string")
                    nomenclature_type = raw.get("ТипНоменклатуры")
                    if (
                        not isinstance(nomenclature_type, str)
                        or not nomenclature_type.strip()
                        or len(nomenclature_type.strip()) > 100
                    ):
                        raise ODataPreviewError("1C nomenclature type is invalid")
                    item["article"] = (article or "").strip()[:120]
                    item["nomenclature_type"] = nomenclature_type.strip()
                found[key] = item
    if set(found) != expected:
        raise ODataPreviewError("1C reference is missing or unavailable")
    return found


def _document_date(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ODataPreviewError("1C document date is invalid")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ODataPreviewError("1C document date is invalid") from exc


def _read_document_entities(
    config,
    refs,
    *,
    opener,
    page_budget,
    require_all,
):
    by_type = defaultdict(set)
    for entity_type, guid in refs:
        if entity_type not in DOCUMENTS or entity_type not in PROFIT_DOCUMENT_TYPES:
            raise ODataPreviewError("1C document type is not allowed")
        by_type[entity_type].add(guid)
    documents = {}
    for entity_type, guids in sorted(by_type.items()):
        fields = DOCUMENTS[entity_type]["fields"]
        for batch_guids in _chunks(sorted(guids)):
            url = _reference_url(config, entity_type, fields, batch_guids)
            returned = 0
            for raw_rows, _ in read_odata_pages(config, url, opener=opener):
                page_budget["used"] += 1
                if page_budget["used"] > config.max_pages:
                    raise ODataPreviewError(
                        "1C document lookups exceeded the page limit"
                    )
                returned += len(raw_rows)
                if returned > len(batch_guids):
                    raise ODataPreviewError(
                        "1C document lookup returned unexpected rows"
                    )
                for raw in raw_rows:
                    if not isinstance(raw, dict):
                        raise ODataPreviewError("1C document row must be an object")
                    key = normalize_guid(raw.get("Ref_Key"), field="Document Ref_Key")
                    identity = entity_type, key
                    if key not in batch_guids or identity in documents:
                        raise ODataPreviewError(
                            "1C document lookup returned an unexpected identity"
                        )
                    number = raw.get("Number")
                    if (
                        not isinstance(number, str)
                        or not number.strip()
                        or len(number.strip()) > 100
                    ):
                        raise ODataPreviewError("1C document number is invalid")
                    item = {
                        "number": number.strip(),
                        "date": _document_date(raw.get("Date")),
                    }
                    if entity_type == "Document_РасходнаяНакладная":
                        raw_order = raw.get("Заказ")
                        raw_order_type = raw.get("Заказ_Type")
                        if raw_order not in (None, "", ZERO_GUID):
                            order_guid = normalize_guid(
                                raw_order, field="Document Заказ", allow_zero=True
                            )
                            if order_guid != ZERO_GUID:
                                order_type = normalize_document_type(
                                    raw_order_type,
                                    field="Document Заказ_Type",
                                    allowed_types={ORDER_TYPE},
                                )
                                item["order_ref"] = (order_type, order_guid)
                        elif raw_order_type not in (None, ""):
                            if raw_order == ZERO_GUID:
                                normalize_document_type(
                                    raw_order_type,
                                    field="Document Заказ_Type",
                                    allowed_types={ORDER_TYPE},
                                )
                            else:
                                raise ODataPreviewError(
                                    "Document Заказ_Type requires a non-empty Заказ"
                                )
                    documents[identity] = item
    missing = set(refs) - set(documents)
    if require_all and missing:
        raise ODataPreviewError("1C document is missing or unavailable")
    return documents


def _read_profit_documents(config, rows, *, opener, page_budget):
    """Resolve only the fixed document schema used by the profit import."""
    primary_refs = {
        (row.recorder_type, row.recorder)
        for row in rows
        if row.recorder_type in PROFIT_RECORDER_TYPES
    }
    documents = _read_document_entities(
        config,
        primary_refs,
        opener=opener,
        page_budget=page_budget,
        require_all=True,
    )
    order_refs = {
        item["order_ref"]
        for item in documents.values()
        if item.get("order_ref")
    }
    if order_refs:
        documents.update(_read_document_entities(
            config,
            order_refs,
            opener=opener,
            page_budget=page_budget,
            require_all=False,
        ))
    return documents


def _document_display(entity_type, document):
    return (
        f'{DOCUMENTS[entity_type]["label"]} №{document["number"]} '
        f'от {document["date"]:%d.%m.%Y}'
    )


def _group_key(organization_id, entity_type, recorder):
    return f"odata-document:{organization_id}:{entity_type}:{recorder}"


def _document_groups(rows, documents, organization_id):
    reports_by_day = defaultdict(set)
    for row in rows:
        if row.recorder_type != RETAIL_REPORT_TYPE:
            continue
        identity = row.recorder_type, row.recorder
        document = documents.get(identity)
        if document is None:
            continue
        reports_by_day[(row.organization_guid, document["date"])].add(identity)

    groups = {}
    for row in rows:
        primary_identity = row.recorder_type, row.recorder
        group_identity = primary_identity
        primary_document = documents.get(primary_identity)
        if row.recorder_type == RETAIL_CHECK_TYPE and primary_document is not None:
            candidates = reports_by_day.get(
                (row.organization_guid, primary_document["date"]), set()
            )
            if len(candidates) == 1:
                group_identity = next(iter(candidates))
        group_type, group_recorder = group_identity
        group_document = documents.get(group_identity)
        display = (
            _document_display(group_type, group_document)
            if group_document is not None
            else f"Документ 1С от {row.source_date:%d.%m.%Y}"
        )
        groups[row.identity] = {
            "group_recorder": group_recorder,
            "group_recorder_type": group_type,
            "group_key": _group_key(
                organization_id, group_type, group_recorder
            ),
            "display": display,
            "group_document": group_document,
            "primary_document": primary_document,
        }
    return groups


def _source_row(row: ProfitRow):
    return {
        "recorder": row.recorder,
        "recorder_type": row.recorder_type,
        "line_number": row.line_number,
        "period": row.source_period,
        "source_date": row.source_date.isoformat(),
        "organization_guid": row.organization_guid,
        "nomenclature_guid": row.nomenclature_guid,
        "customer_guid": row.customer_guid,
        "responsible_guid": row.responsible_guid,
        "document_guid": row.document_guid,
        "document_type": row.document_type,
        "quantity": format(row.quantity, "f"),
        "revenue": format(row.revenue, "f"),
        "vat": format(row.vat, "f"),
        "cost": format(row.cost, "f"),
    }


def _snapshot_bytes(payload):
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _save_batch_snapshot(batch, payload):
    content = _snapshot_bytes(payload)
    batch.file_sha256 = hashlib.sha256(content).hexdigest()
    batch.file_size = len(content)
    generated_name = batch.stored_file.field.generate_filename(batch, "snapshot.json")
    batch.stored_file.name = batch.stored_file.storage.save(
        generated_name, ContentFile(content)
    )
    try:
        batch.save()
    except Exception:
        delete_private_batch_file(batch)
        raise
    return batch


def _failed_mapping_batch(
    rows, start_month, end_month, scope_months, organizations,
    organization, user, message,
):
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "start_month": start_month,
        "end_month": end_month,
        "scope_months": [month.isoformat() for month in scope_months],
        "organization_guids": list(organizations),
        "rows": [_source_row(row) for row in rows],
    }
    batch = OneCImportBatch(
        organization=organization,
        source_type=OneCImportBatch.SOURCE_ODATA,
        original_filename=f"onec-odata-{start_month}-{end_month}.json",
        status=OneCImportBatch.STATUS_FAILED,
        uploaded_by=user,
        parser_version=PARSER_VERSION,
        rows_detected=len(rows),
        error_message=message[:ERROR_MESSAGE_MAX_LENGTH],
        period_first=parse_month(start_month),
        period_last=parse_month(end_month),
        metadata={"source": "odata", "critical_errors": [message[:300]]},
    )
    try:
        _save_batch_snapshot(batch, payload)
    except IntegrityError as exc:
        raise ODataDraftError("An identical OData snapshot already exists") from exc
    _audit(batch, user, {"status": "uploaded"}, {"status": "failed"})
    return batch


def _enrich_rows(rows, references, documents, organization_id):
    normalized = []
    groups = _document_groups(rows, documents, organization_id)
    for row in rows:
        group = groups[row.identity]
        primary_document = group["primary_document"]
        nomenclature = references["nomenclature"][row.nomenclature_guid]
        customer_name = (
            "Без контрагента"
            if row.customer_guid == ZERO_GUID
            else references["customer"][row.customer_guid]["description"]
        )
        revenue = row.revenue.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        cost = row.cost.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        gross_profit = revenue - cost
        quantity = row.quantity.quantize(QUANTITY_QUANTUM, rounding=ROUND_HALF_UP)
        normalized.append({
            "period_month": row.source_date.replace(day=1).isoformat(),
            "source_recorder": row.recorder,
            "source_row_number": row.line_number,
            "source_identity": onec_monthly_profit_source_identity(
                period_month=row.source_date.replace(day=1),
                source_row_number=row.line_number,
                source_recorder=row.recorder,
            ),
            "manager_name": references["responsible"][row.responsible_guid]["description"],
            "customer_name": customer_name,
            "document_name": group["display"],
            "nomenclature": nomenclature["description"],
            "article": nomenclature.get("article", ""),
            "nomenclature_type": nomenclature["nomenclature_type"],
            "quantity": format(quantity, "f"),
            "revenue": format(revenue, "f"),
            "cost": format(cost, "f"),
            "gross_profit": format(gross_profit, "f"),
            "calculated_cost": None,
            "cost_source": OneCMonthlyProfit.COST_SOURCE_ACTUAL,
            "cost_calculation_method": "",
            "cost_calculation_ratio": None,
            "analytical_gross_profit": format(gross_profit, "f"),
            "profitability_percent": (
                format(calculate_profitability(gross_profit, revenue), "f") if revenue else None
            ),
            "source_data": {
                "source": "odata",
                "recorder": row.recorder,
                "recorder_type": row.recorder_type,
                "line_number": row.line_number,
                "period": row.source_period,
                "source_date": row.source_date.isoformat(),
                "organization_guid": row.organization_guid,
                "nomenclature_guid": row.nomenclature_guid,
                "nomenclature_type": nomenclature["nomenclature_type"],
                "customer_guid": row.customer_guid,
                "responsible_guid": row.responsible_guid,
                "vat": format(row.vat, "f"),
                "document_guid": row.document_guid,
                "document_type": row.document_type,
                "document_group_recorder": group["group_recorder"],
                "document_group_recorder_type": group["group_recorder_type"],
                "document_group_key": group["group_key"],
                "document_display": group["display"],
            },
        })
        if primary_document is not None:
            normalized[-1]["source_data"].update({
                "document_number": primary_document["number"],
                "document_date": primary_document["date"].isoformat(),
            })
        group_document = group["group_document"]
        if group_document is not None:
            normalized[-1]["source_data"].update({
                "document_group_number": group_document["number"],
                "document_group_date": group_document["date"].isoformat(),
            })
        order_ref = primary_document.get("order_ref") if primary_document else None
        if order_ref and order_ref in documents:
            order_document = documents[order_ref]
            normalized[-1]["source_data"].update({
                "resolved_order_guid": order_ref[1],
                "resolved_order_type": order_ref[0],
                "resolved_order_number": order_document["number"],
                "resolved_order_date": order_document["date"].isoformat(),
                "resolved_order_display": _document_display(
                    order_ref[0], order_document
                ),
            })
    return normalized


def _decimal_from_snapshot(value, field, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"Snapshot {field} must be a decimal string.")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError(f"Snapshot {field} is invalid.") from exc
    if not parsed.is_finite():
        raise ValidationError(f"Snapshot {field} must be finite.")
    return parsed


def _reject_guid_label(value):
    try:
        UUID(value.strip())
    except (ValueError, TypeError, AttributeError):
        return
    raise ValidationError("OData snapshot contains a GUID instead of a display name.")


def _validate_decimal_shape(value, field, *, decimal_places, integer_places):
    quantum = Decimal(1).scaleb(-decimal_places)
    if value != value.quantize(quantum):
        raise ValidationError(f"Snapshot {field} has unsupported precision.")
    if abs(value) >= Decimal(10) ** integer_places:
        raise ValidationError(f"Snapshot {field} is outside the supported range.")


def _snapshot_document_type(value, *, field, allowed_types=None):
    if not isinstance(value, str):
        raise ValidationError(f"{field} is invalid.")
    try:
        normalized = normalize_document_type(
            f"StandardODATA.{value}",
            field=field,
            allowed_types=allowed_types,
        )
    except ODataPreviewError as exc:
        raise ValidationError(f"{field} is invalid.") from exc
    if normalized != value:
        raise ValidationError(f"{field} is invalid.")
    return normalized


def _validate_snapshot(payload, config, *, organization_id):
    if not isinstance(payload, dict) or payload.get("schema") != SNAPSHOT_SCHEMA:
        raise ValidationError("OData snapshot schema is invalid.")
    try:
        scope_months = _month_scope(payload.get("start_month"), payload.get("end_month"))
    except ODataDraftError as exc:
        raise ValidationError(exc.messages) from exc
    start = scope_months[0]
    end = scope_months[-1]
    expected_scope = [month.isoformat() for month in scope_months]
    if payload.get("scope_months") != expected_scope:
        raise ValidationError("OData snapshot month scope is invalid.")
    organizations = tuple(
        normalize_guid(value, field="Snapshot organization GUID")
        for value in payload.get("organization_guids", [])
    )
    if not organizations or not set(organizations).issubset(config.organization_guids):
        raise ValidationError("OData snapshot organization is not allowed.")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > config.max_rows:
        raise ValidationError("OData snapshot row limit is invalid.")
    seen = set()
    rows = []
    retail_reports = defaultdict(dict)
    retail_links = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValidationError("OData snapshot row is invalid.")
        try:
            recorder = str(UUID(str(raw.get("source_recorder")))).lower()
            line = int(raw.get("source_row_number"))
            period = date.fromisoformat(raw.get("period_month"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("OData snapshot identity or period is invalid.") from exc
        if line < 0 or line > 2147483647 or period.day != 1 or not start <= period <= end:
            raise ValidationError("OData snapshot row is outside its period.")
        identity = recorder, line
        if identity in seen:
            raise ValidationError("OData snapshot contains duplicate source identity.")
        seen.add(identity)
        expected_source_identity = onec_monthly_profit_source_identity(
            period_month=period,
            source_row_number=line,
            source_recorder=recorder,
        )
        if raw.get("source_identity") != expected_source_identity:
            raise ValidationError("OData snapshot source identity is invalid.")
        source_data = raw.get("source_data")
        if not isinstance(source_data, dict):
            raise ValidationError("OData snapshot audit data is invalid.")
        source_org = normalize_guid(
            source_data.get("organization_guid"), field="Snapshot row organization"
        )
        if source_org not in organizations:
            raise ValidationError("OData snapshot row organization is not allowed.")
        for name in ("nomenclature", "manager_name"):
            if not isinstance(raw.get(name), str) or not raw[name].strip():
                raise ValidationError("OData snapshot contains an unresolved reference.")
            _reject_guid_label(raw[name])
        if len(raw["nomenclature"]) > 500 or len(raw["manager_name"]) > 300:
            raise ValidationError("OData snapshot display name is too long.")
        customer = raw.get("customer_name")
        if not isinstance(customer, str) or not customer.strip():
            raise ValidationError("OData snapshot contains an unresolved reference.")
        _reject_guid_label(customer)
        if len(customer) > 500:
            raise ValidationError("OData snapshot display name is too long.")
        nomenclature_type = raw.get("nomenclature_type")
        if (
            not isinstance(nomenclature_type, str)
            or not nomenclature_type.strip()
            or len(nomenclature_type) > 100
        ):
            raise ValidationError("OData snapshot nomenclature type is invalid.")
        if source_data.get("nomenclature_type") != nomenclature_type:
            raise ValidationError("OData snapshot nomenclature type does not match audit data.")
        document = raw.get("document_name") or ""
        article = raw.get("article") or ""
        if not isinstance(document, str) or not isinstance(article, str):
            raise ValidationError("OData snapshot display fields are invalid.")
        if len(document) > 500 or len(article) > 120:
            raise ValidationError("OData snapshot display field is too long.")
        if source_data.get("source") != "odata":
            raise ValidationError("OData snapshot audit source is invalid.")
        try:
            audit_recorder = str(UUID(str(source_data.get("recorder")))).lower()
            audit_line = int(source_data.get("line_number"))
            source_date = date.fromisoformat(source_data.get("source_date"))
            source_period_value = source_data.get("period")
            if not isinstance(source_period_value, str) or len(source_period_value) > 80:
                raise ValueError
            source_period_date = datetime.fromisoformat(
                source_period_value.replace("Z", "+00:00")
            ).date()
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("OData snapshot audit identity is invalid.") from exc
        if (
            audit_recorder != recorder
            or audit_line != line
            or source_date.replace(day=1) != period
            or source_period_date != source_date
        ):
            raise ValidationError("OData snapshot audit identity does not match its row.")
        recorder_type = _snapshot_document_type(
            source_data.get("recorder_type"), field="Snapshot Recorder_Type"
        )
        document_guid = source_data.get("document_guid")
        document_type = source_data.get("document_type")
        if document_guid is None:
            if document_type is not None:
                raise ValidationError("OData snapshot document identity is invalid.")
        else:
            normalize_guid(
                document_guid, field="Snapshot document GUID"
            )
            _snapshot_document_type(
                document_type, field="Snapshot Документ_Type"
            )
        group_recorder = normalize_guid(
            source_data.get("document_group_recorder"),
            field="Snapshot document group recorder",
        )
        group_type = _snapshot_document_type(
            source_data.get("document_group_recorder_type"),
            field="Snapshot document group recorder type",
        )
        if source_data.get("document_group_key") != _group_key(
            organization_id, group_type, group_recorder
        ):
            raise ValidationError("OData snapshot document group key is invalid.")
        if (group_type, group_recorder) != (recorder_type, recorder):
            if not (
                recorder_type == RETAIL_CHECK_TYPE
                and group_type == RETAIL_REPORT_TYPE
            ):
                raise ValidationError("OData snapshot document group is invalid.")
        document_display = source_data.get("document_display")
        if (
            not isinstance(document_display, str)
            or not document_display.strip()
            or len(document_display) > 500
            or document != document_display
        ):
            raise ValidationError("OData snapshot document display is invalid.")
        known_recorder = recorder_type in PROFIT_RECORDER_TYPES
        document_number = source_data.get("document_number")
        document_date_value = source_data.get("document_date")
        group_number = source_data.get("document_group_number")
        group_date_value = source_data.get("document_group_date")
        if known_recorder:
            if not isinstance(document_number, str) or not document_number.strip():
                raise ValidationError("OData snapshot document number is invalid.")
            if len(document_number) > 100:
                raise ValidationError("OData snapshot document number is invalid.")
            try:
                document_date = date.fromisoformat(document_date_value)
                group_date = date.fromisoformat(group_date_value)
            except (TypeError, ValueError) as exc:
                raise ValidationError("OData snapshot document date is invalid.") from exc
            if not isinstance(group_number, str) or not group_number.strip() or len(group_number) > 100:
                raise ValidationError("OData snapshot group document number is invalid.")
            expected_display = _document_display(group_type, {
                "number": group_number,
                "date": group_date,
            })
            if document_display != expected_display:
                raise ValidationError("OData snapshot document display is inconsistent.")
            if (group_type, group_recorder) == (recorder_type, recorder):
                if group_number != document_number or group_date != document_date:
                    raise ValidationError(
                        "OData snapshot self-group document is inconsistent."
                    )
            if recorder_type == RETAIL_REPORT_TYPE:
                if source_date != document_date:
                    raise ValidationError("OData snapshot retail report date is inconsistent.")
                report_identity = (recorder_type, recorder)
                report_descriptor = (
                    document_number,
                    document_date,
                    document_display,
                )
                existing_descriptor = retail_reports[
                    (source_org, document_date)
                ].get(report_identity)
                if (
                    existing_descriptor is not None
                    and existing_descriptor != report_descriptor
                ):
                    raise ValidationError(
                        "OData snapshot retail report display is inconsistent."
                    )
                retail_reports[(source_org, document_date)][
                    report_identity
                ] = report_descriptor
            if recorder_type == RETAIL_CHECK_TYPE and group_type == RETAIL_REPORT_TYPE:
                if source_date != document_date or document_date != group_date:
                    raise ValidationError("OData snapshot retail document date is inconsistent.")
                retail_links.append((
                    source_org,
                    document_date,
                    (group_type, group_recorder),
                    (group_number, group_date, document_display),
                ))
        elif any(value is not None for value in (
            document_number, document_date_value, group_number, group_date_value
        )):
            raise ValidationError("OData snapshot unsupported document was enriched.")
        order_values = tuple(source_data.get(name) for name in (
            "resolved_order_guid", "resolved_order_type", "resolved_order_number",
            "resolved_order_date", "resolved_order_display",
        ))
        if any(value is not None for value in order_values):
            if any(value is None for value in order_values):
                raise ValidationError("OData snapshot resolved order is incomplete.")
            order_guid, order_type, order_number, order_date_value, order_display = order_values
            normalize_guid(order_guid, field="Snapshot resolved order GUID")
            if _snapshot_document_type(
                order_type,
                field="Snapshot resolved order type",
                allowed_types={ORDER_TYPE},
            ) != ORDER_TYPE:
                raise ValidationError("OData snapshot resolved order type is invalid.")
            if not isinstance(order_number, str) or not order_number.strip() or len(order_number) > 100:
                raise ValidationError("OData snapshot resolved order number is invalid.")
            try:
                order_date = date.fromisoformat(order_date_value)
            except (TypeError, ValueError) as exc:
                raise ValidationError("OData snapshot resolved order date is invalid.") from exc
            if order_display != _document_display(ORDER_TYPE, {
                "number": order_number,
                "date": order_date,
            }):
                raise ValidationError("OData snapshot resolved order display is invalid.")
        normalize_guid(source_data.get("nomenclature_guid"), field="Snapshot nomenclature")
        normalize_guid(
            source_data.get("customer_guid"), field="Snapshot customer", allow_zero=True
        )
        normalize_guid(source_data.get("responsible_guid"), field="Snapshot responsible")
        quantity = _decimal_from_snapshot(raw.get("quantity"), "quantity")
        revenue = _decimal_from_snapshot(raw.get("revenue"), "revenue")
        cost = _decimal_from_snapshot(raw.get("cost"), "cost")
        gross_profit = _decimal_from_snapshot(raw.get("gross_profit"), "gross_profit")
        analytical_profit = _decimal_from_snapshot(
            raw.get("analytical_gross_profit"), "analytical_gross_profit"
        )
        profitability = _decimal_from_snapshot(
            raw.get("profitability_percent"), "profitability_percent", nullable=True
        )
        vat = _decimal_from_snapshot(source_data.get("vat"), "vat")
        _validate_decimal_shape(quantity, "quantity", decimal_places=6, integer_places=14)
        for field, value in (
            ("revenue", revenue), ("cost", cost), ("gross_profit", gross_profit),
            ("analytical_gross_profit", analytical_profit), ("vat", vat),
        ):
            _validate_decimal_shape(value, field, decimal_places=2, integer_places=18)
        if gross_profit != revenue - cost or analytical_profit != gross_profit:
            raise ValidationError("OData snapshot profit values are inconsistent.")
        expected_profitability = calculate_profitability(gross_profit, revenue)
        if profitability != expected_profitability:
            raise ValidationError("OData snapshot profitability is inconsistent.")
        if profitability is not None:
            _validate_decimal_shape(
                profitability, "profitability_percent", decimal_places=4, integer_places=8
            )
        if raw.get("calculated_cost") is not None or raw.get("cost_calculation_ratio") is not None:
            raise ValidationError("OData snapshot must use its source cost.")
        rows.append({
            "period_month": period,
            "source_recorder": recorder,
            "source_row_number": line,
            "source_identity": expected_source_identity,
            "manager_name": raw["manager_name"],
            "customer_name": customer,
            "document_name": document,
            "nomenclature": raw["nomenclature"],
            "article": article,
            "nomenclature_type": nomenclature_type,
            "quantity": quantity,
            "revenue": revenue,
            "cost": cost,
            "gross_profit": gross_profit,
            "calculated_cost": None,
            "cost_source": OneCMonthlyProfit.COST_SOURCE_ACTUAL,
            "cost_calculation_method": "",
            "cost_calculation_ratio": None,
            "analytical_gross_profit": analytical_profit,
            "profitability_percent": profitability,
            "source_data": source_data,
        })
    for source_org, document_date, target, descriptor in retail_links:
        candidates = retail_reports[(source_org, document_date)]
        if set(candidates) != {target}:
            raise ValidationError("OData snapshot retail document group is ambiguous.")
        if candidates[target] != descriptor:
            raise ValidationError(
                "OData snapshot retail target display is inconsistent."
            )
    return rows, scope_months


def _month_totals(rows):
    totals = defaultdict(lambda: {
        "row_count": 0, "quantity": Decimal("0"), "revenue": Decimal("0"),
        "vat": Decimal("0"), "cost": Decimal("0"),
        "gross_profit": Decimal("0"),
    })
    for row in rows:
        month = row["period_month"] if isinstance(row["period_month"], date) else date.fromisoformat(row["period_month"])
        item = totals[month]
        item["row_count"] += 1
        item["quantity"] += Decimal(row["quantity"])
        item["revenue"] += Decimal(row["revenue"])
        item["vat"] += Decimal(row["source_data"]["vat"])
        item["cost"] += Decimal(row["cost"])
        item["gross_profit"] += Decimal(row["analytical_gross_profit"])
    return totals


def _preview_metadata(rows, organization, scope_months):
    draft = _month_totals(rows)
    months = list(scope_months)
    active_states = {
        state.period_month: state.active_batch_id
        for state in OneCReportPeriodState.objects.filter(
            organization=organization,
            report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            period_month__in=months,
        )
    }
    active_rows = OneCMonthlyProfit.objects.active_for(organization).filter(
        period_month__in=months
    )
    active = defaultdict(lambda: {
        "row_count": 0, "revenue": Decimal("0"), "cost": Decimal("0"),
        "gross_profit": Decimal("0"),
    })
    for row in active_rows.iterator():
        item = active[row.period_month]
        item["row_count"] += 1
        item["revenue"] += row.revenue or Decimal("0")
        item["cost"] += row.analytical_cost or Decimal("0")
        item["gross_profit"] += row.displayed_gross_profit or Decimal("0")
    monthly = []
    for month in months:
        draft_item = draft[month]
        has_active = month in active_states
        active_item = active[month] if has_active else None
        monthly.append({
            "month": month.strftime("%Y-%m"),
            "row_count": draft_item["row_count"],
            "quantity": format(draft_item["quantity"], "f"),
            "revenue": format(draft_item["revenue"], "f"),
            "vat": format(draft_item["vat"], "f"),
            "cost": format(draft_item["cost"], "f"),
            "gross_profit": format(draft_item["gross_profit"], "f"),
            "has_active": has_active,
            "active_revenue": format(active_item["revenue"], "f") if active_item else None,
            "active_cost": format(active_item["cost"], "f") if active_item else None,
            "active_gross_profit": format(active_item["gross_profit"], "f") if active_item else None,
            "revenue_difference": format(draft_item["revenue"] - active_item["revenue"], "f") if active_item else None,
            "cost_difference": format(draft_item["cost"] - active_item["cost"], "f") if active_item else None,
            "gross_profit_difference": format(draft_item["gross_profit"] - active_item["gross_profit"], "f") if active_item else None,
        })
    total = {
        key: sum((item[key] for item in draft.values()), Decimal("0"))
        for key in ("quantity", "revenue", "vat", "cost", "gross_profit")
    }
    total["row_count"] = sum(item["row_count"] for item in draft.values())
    total["profitability_percent"] = calculate_profitability(
        total["gross_profit"], total["revenue"]
    )
    return {
        "source": "odata",
        "scope_months": [month.isoformat() for month in months],
        "report": {
            "layout": "odata",
            "month_count": len(months),
            "months": [month.isoformat() for month in months],
        },
        "monthly": monthly,
        "totals": {key: format(value, "f") if isinstance(value, Decimal) else value for key, value in total.items()},
        "overlap_months": [item["month"] for item in monthly if item["has_active"]],
        "overlap_count": sum(1 for item in monthly if item["has_active"]),
        "critical_errors": [],
        "warnings": [],
        "preview": rows[:30],
    }


def create_odata_profit_draft(start_month, end_month, organization, user, *, config=None, opener=None):
    _require_odata_target_organization(organization)
    scope_months = _month_scope(start_month, end_month)
    config = validate_config(config or config_from_settings())
    client = opener or build_opener(NoRedirectHandler())
    rows, page_count = read_profit_rows(
        config, start_month, end_month, opener=client
    )
    required = {
        "nomenclature": {row.nomenclature_guid for row in rows},
        "customer": {row.customer_guid for row in rows if row.customer_guid != ZERO_GUID},
        "responsible": {row.responsible_guid for row in rows},
    }
    try:
        reference_page_budget = {"used": 0}
        references = {
            kind: _read_reference_map(
                config, kind, guids, opener=client,
                page_budget=reference_page_budget,
            )
            for kind, guids in required.items()
        }
        documents = _read_profit_documents(
            config,
            rows,
            opener=client,
            page_budget=reference_page_budget,
        )
        normalized = _enrich_rows(rows, references, documents, organization.pk)
    except ODataPreviewError as exc:
        safe_message = str(exc)[:ERROR_MESSAGE_MAX_LENGTH]
        batch = _failed_mapping_batch(
            rows, start_month, end_month, scope_months, config.organization_guids,
            organization, user, safe_message,
        )
        raise ODataDraftError(safe_message, batch=batch) from exc
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "start_month": start_month,
        "end_month": end_month,
        "scope_months": [month.isoformat() for month in scope_months],
        "organization_guids": list(config.organization_guids),
        "page_count": page_count,
        "rows": normalized,
    }
    try:
        _validate_snapshot(payload, config, organization_id=organization.pk)
    except (ValidationError, ODataPreviewError) as exc:
        raise ODataDraftError("OData response cannot be saved as a valid draft") from exc
    metadata = _preview_metadata(normalized, organization, scope_months)
    batch = OneCImportBatch(
        organization=organization,
        source_type=OneCImportBatch.SOURCE_ODATA,
        original_filename=f"onec-odata-{start_month}-{end_month}.json",
        status=OneCImportBatch.STATUS_PREVIEWED,
        uploaded_by=user,
        parser_version=PARSER_VERSION,
        rows_detected=len(normalized),
        period_first=parse_month(start_month),
        period_last=parse_month(end_month),
        metadata=metadata,
    )
    try:
        _save_batch_snapshot(batch, payload)
    except IntegrityError as exc:
        raise ODataDraftError("An identical OData snapshot already exists") from exc
    _audit(batch, user, {"status": "uploaded"}, {"status": "previewed"})
    return batch


def _read_snapshot(batch):
    digest = hashlib.sha256()
    content = bytearray()
    with batch.stored_file.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            content.extend(chunk)
    if digest.hexdigest() != batch.file_sha256:
        raise ValidationError("OData snapshot checksum has changed.")
    try:
        return json.loads(bytes(content).decode("utf-8"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("OData snapshot JSON is invalid.") from exc


def confirm_odata_profit(batch_id, organization, user, *, config=None):
    _require_odata_target_organization(organization)
    config = validate_config(config or config_from_settings())
    try:
        with transaction.atomic():
            locked_organization = Organization.objects.select_for_update().get(pk=organization.pk)
            batch = OneCImportBatch.objects.select_for_update().get(
                id=batch_id,
                organization=locked_organization,
                source_type=OneCImportBatch.SOURCE_ODATA,
                sync_run__isnull=True,
            )
            if batch.status != OneCImportBatch.STATUS_PREVIEWED:
                raise ValidationError("Only a previewed OData draft can be confirmed.")
            if batch.parser_version != PARSER_VERSION:
                raise ValidationError("OData draft version is no longer supported.")
            payload = _read_snapshot(batch)
            if (
                batch.period_first != parse_month(payload.get("start_month"))
                or batch.period_last != parse_month(payload.get("end_month"))
            ):
                raise ValidationError("OData draft period does not match its snapshot.")
            records, periods = _validate_snapshot(
                payload, config, organization_id=locked_organization.pk
            )
            locked_states = list(
                OneCReportPeriodState.objects.select_for_update()
                .filter(
                    organization=locked_organization,
                    report_type=batch.import_type,
                    period_month__in=periods,
                )
                .select_related("active_batch")
                .order_by("period_month")
            )
            rows = [
                OneCMonthlyProfit(
                    import_batch=batch,
                    organization=locked_organization,
                    **record,
                )
                for record in records
            ]
            _bulk_create_monthly_rows(rows)
            before = {"status": batch.status, "rows_imported": batch.rows_imported}
            batch.status = OneCImportBatch.STATUS_CONFIRMED
            _activate_period_states(
                batch, locked_organization, user, periods, locked_states
            )
            _save_confirmed_batch(batch, user, len(rows))
            _audit(batch, user, before, {
                "status": batch.status, "rows_imported": batch.rows_imported,
            })
        return batch
    except OneCImportBatch.DoesNotExist:
        raise
    except Exception as exc:
        safe_message = (
            f"OData draft confirmation failed: {type(exc).__name__}."
        )[:ERROR_MESSAGE_MAX_LENGTH]
        updated = OneCImportBatch.objects.filter(
            id=batch_id,
            organization=organization,
            source_type=OneCImportBatch.SOURCE_ODATA,
            status=OneCImportBatch.STATUS_PREVIEWED,
            sync_run__isnull=True,
        ).update(status=OneCImportBatch.STATUS_FAILED, error_message=safe_message)
        if updated:
            failed_batch = OneCImportBatch.objects.get(
                id=batch_id, organization=organization
            )
            _audit(
                failed_batch, user, {"status": "previewed"}, {"status": "failed"}
            )
        raise
