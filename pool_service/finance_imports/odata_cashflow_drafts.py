"""Reviewed draft and explicit confirmation for 1C OData cash flow."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import uuid
from urllib.parse import quote
from urllib.request import build_opener
from uuid import UUID

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction

from pool_service.models import (
    CashFlowRow,
    OneCImportBatch,
    OneCReportPeriodState,
    Organization,
    cashflow_source_identity,
)
from .employee_matching import normalize_onec_name
from .odata_cashflow import CashFlowODataRow, read_cashflow_rows
from .odata_profit import (
    NoRedirectHandler,
    ODataPreviewError,
    ZERO_GUID,
    normalize_guid,
    parse_month,
    read_odata_pages,
    validate_config,
)
from .odata_profit_drafts import (
    config_from_settings,
    is_odata_target_organization,
)
from .services import (
    ERROR_MESSAGE_MAX_LENGTH,
    _activate_period_states,
    _audit,
    _save_confirmed_batch,
)
from .validators import delete_private_batch_file


SNAPSHOT_SCHEMA = "onec_odata_cashflow_draft_v1"
PARSER_VERSION = "odata-cashflow-1"
ARTICLE_ENTITY_SET = "Catalog_СтатьиДвиженияДенежныхСредств"
ARTICLE_FIELDS = ("Ref_Key", "Description", "DeletionMark", "Недействителен")
REFERENCE_BATCH_SIZE = 40
MAX_DRAFT_MONTHS = 12
NO_ARTICLE_LABEL = "Без статьи 1С"
MONEY_QUANTUM = Decimal("0.01")


class ODataCashFlowDraftError(ValidationError):
    def __init__(self, message, *, batch=None):
        self.batch = batch
        super().__init__(message)


def _require_target(organization):
    if not is_odata_target_organization(organization):
        raise ODataCashFlowDraftError(
            "OData import is not configured for this organization"
        )


def _month_scope(start_month, end_month):
    start = parse_month(start_month)
    end = parse_month(end_month)
    if end < start:
        raise ODataCashFlowDraftError(
            "Конечный месяц не может быть раньше начального."
        )
    count = (end.year - start.year) * 12 + end.month - start.month + 1
    if count > MAX_DRAFT_MONTHS:
        raise ODataCashFlowDraftError(
            "За один раз можно получить не более 12 месяцев."
        )
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


def _reject_guid_label(value):
    try:
        UUID(value.strip())
    except (ValueError, TypeError, AttributeError):
        return
    raise ODataPreviewError("Article Description must not be a GUID")


def _article_url(config, guids):
    filters = " or ".join(f"Ref_Key eq guid'{guid}'" for guid in guids)
    query = "&".join((
        f"$select={quote(','.join(ARTICLE_FIELDS))}",
        f"$filter={quote(filters)}",
    ))
    return f"{config.base_url}{quote(ARTICLE_ENTITY_SET, safe='')}?{query}"


def _read_articles(config, guids, *, opener, page_budget):
    resolved = {}
    ordered = sorted(set(guids))
    for offset in range(0, len(ordered), REFERENCE_BATCH_SIZE):
        batch = ordered[offset:offset + REFERENCE_BATCH_SIZE]
        initial_url = _article_url(config, batch)
        for raw_rows, _ in read_odata_pages(config, initial_url, opener=opener):
            page_budget["used"] += 1
            if page_budget["used"] > config.max_pages:
                raise ODataPreviewError(
                    "OData reference lookup exceeded the configured page limit"
                )
            for raw in raw_rows:
                if not isinstance(raw, dict):
                    raise ODataPreviewError("Article catalog row must be an object")
                guid = normalize_guid(raw.get("Ref_Key"), field="Article Ref_Key")
                if guid not in batch:
                    raise ODataPreviewError("Article lookup returned an unexpected reference")
                if raw.get("DeletionMark") is not False:
                    raise ODataPreviewError("Article reference is deleted or unverified")
                if raw.get("Недействителен") is not False:
                    raise ODataPreviewError("Article reference is inactive or unverified")
                description = raw.get("Description")
                if not isinstance(description, str) or not description.strip():
                    raise ODataPreviewError("Article reference has no Description")
                description = description.strip()
                if len(description) > 500:
                    raise ODataPreviewError("Article Description is too long")
                _reject_guid_label(description)
                if guid in resolved:
                    raise ODataPreviewError("Article lookup returned a duplicate reference")
                resolved[guid] = description
        missing = set(batch) - set(resolved)
        if missing:
            raise ODataPreviewError("Article reference was not returned by 1C")
    return resolved


def _normalise_rows(rows, articles):
    normalized = []
    warnings = []
    for row in rows:
        article = articles.get(row.article_guid) if row.article_guid else NO_ARTICLE_LABEL
        if row.article_guid is None:
            warnings.append(
                f"{row.source_date.isoformat()}: движение без статьи 1С сохранено в итогах."
            )
        source_identity = cashflow_source_identity(
            period_month=row.source_date.replace(day=1),
            source_row_number=row.line_number,
            source_recorder=row.recorder,
            source_recorder_type=row.recorder_type,
        )
        normalized.append({
            "period_month": row.source_date.replace(day=1).isoformat(),
            "source_row_number": row.line_number,
            "source_identity": source_identity,
            "source_reference": "",
            "article_raw": article,
            "normalized_article_name": normalize_onec_name(article),
            "document_raw": row.dimensions["analytics"],
            "receipts": format(row.receipts, "f"),
            "payments": format(row.payments, "f"),
            "net_cash_flow": format(row.net_cash_flow, "f"),
            "source_data": {
                "source": "odata",
                "recorder": row.recorder,
                "recorder_type": row.recorder_type,
                "line_number": row.line_number,
                "source_date": row.source_date.isoformat(),
                "organization_guid": row.organization_guid,
                "article_guid": row.article_guid or "",
                **row.dimensions,
            },
        })
    return normalized, warnings


def _money(value, field):
    if not isinstance(value, str):
        raise ValidationError(f"Snapshot {field} must be a decimal string.")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValidationError(f"Snapshot {field} is invalid.") from exc
    if not parsed.is_finite() or parsed != parsed.quantize(MONEY_QUANTUM):
        raise ValidationError(f"Snapshot {field} must be a finite two-decimal value.")
    if abs(parsed) >= Decimal("1e18"):
        raise ValidationError(f"Snapshot {field} is outside the supported range.")
    return parsed


def _validate_snapshot(payload, config):
    if not isinstance(payload, dict) or payload.get("schema") != SNAPSHOT_SCHEMA:
        raise ValidationError("OData cash-flow snapshot schema is invalid.")
    try:
        scope = _month_scope(payload.get("start_month"), payload.get("end_month"))
    except (ODataCashFlowDraftError, ODataPreviewError) as exc:
        raise ValidationError("OData cash-flow snapshot period is invalid.") from exc
    expected_scope = [month.isoformat() for month in scope]
    if payload.get("scope_months") != expected_scope:
        raise ValidationError("OData cash-flow snapshot scope is invalid.")
    page_count = payload.get("page_count")
    if (
        isinstance(page_count, bool) or not isinstance(page_count, int)
        or not 1 <= page_count <= config.max_pages
    ):
        raise ValidationError("OData cash-flow snapshot page count is invalid.")
    organizations = tuple(
        normalize_guid(value, field="Snapshot organization GUID")
        for value in payload.get("organization_guids", [])
    )
    if not organizations or not set(organizations).issubset(config.organization_guids):
        raise ValidationError("OData cash-flow snapshot organization is not allowed.")
    raw_rows = payload.get("rows")
    if not isinstance(raw_rows, list) or len(raw_rows) > config.max_rows:
        raise ValidationError("OData cash-flow snapshot row limit is invalid.")
    seen = set()
    records = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise ValidationError("OData cash-flow snapshot row is invalid.")
        try:
            period = date.fromisoformat(raw.get("period_month"))
            line = int(raw.get("source_row_number"))
            source_data = raw.get("source_data")
            source_date = date.fromisoformat(source_data.get("source_date"))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValidationError("OData cash-flow snapshot identity is invalid.") from exc
        if (
            isinstance(raw.get("source_row_number"), (float, bool))
            or line < 0 or period.day != 1 or period.isoformat() not in expected_scope
            or source_date.replace(day=1) != period
        ):
            raise ValidationError("OData cash-flow snapshot row is outside its scope.")
        if not isinstance(source_data, dict) or source_data.get("source") != "odata":
            raise ValidationError("OData cash-flow audit data is invalid.")
        recorder = source_data.get("recorder")
        recorder_type = source_data.get("recorder_type")
        if not isinstance(recorder, str) or not recorder.strip():
            raise ValidationError("OData cash-flow Recorder is invalid.")
        if len(recorder) > 500:
            raise ValidationError("OData cash-flow Recorder is too long.")
        if not isinstance(recorder_type, str) or not recorder_type.strip():
            raise ValidationError("OData cash-flow Recorder_Type is invalid.")
        if len(recorder_type) > 300:
            raise ValidationError("OData cash-flow Recorder_Type is too long.")
        audit_line = source_data.get("line_number")
        if isinstance(audit_line, (float, bool)):
            raise ValidationError("OData cash-flow audit line is invalid.")
        try:
            audit_line = int(audit_line)
        except (TypeError, ValueError) as exc:
            raise ValidationError("OData cash-flow audit line is invalid.") from exc
        if audit_line != line:
            raise ValidationError("OData cash-flow audit identity does not match its row.")
        expected_identity = cashflow_source_identity(
            period_month=period,
            source_row_number=line,
            source_recorder=recorder,
            source_recorder_type=recorder_type,
        )
        if raw.get("source_identity") != expected_identity or expected_identity in seen:
            raise ValidationError("OData cash-flow source identity is invalid or duplicated.")
        seen.add(expected_identity)
        organization_guid = normalize_guid(
            source_data.get("organization_guid"), field="Snapshot row organization"
        )
        if organization_guid not in organizations:
            raise ValidationError("OData cash-flow row organization is not allowed.")
        article = raw.get("article_raw")
        if not isinstance(article, str) or not article.strip() or len(article) > 500:
            raise ValidationError("OData cash-flow article label is invalid.")
        _reject_guid_label(article)
        if raw.get("normalized_article_name") != normalize_onec_name(article):
            raise ValidationError("OData cash-flow normalized article is invalid.")
        article_guid = source_data.get("article_guid")
        if article_guid:
            normalize_guid(article_guid, field="Snapshot article GUID")
            if article == NO_ARTICLE_LABEL:
                raise ValidationError("OData cash-flow article mapping is inconsistent.")
        elif article != NO_ARTICLE_LABEL:
            raise ValidationError("OData cash-flow missing article label is invalid.")
        document = raw.get("document_raw")
        reference = raw.get("source_reference")
        if not isinstance(document, str) or len(document) > 700:
            raise ValidationError("OData cash-flow display data is invalid.")
        dimensions = {
            "cash_type": 700,
            "account_or_cash": 700,
            "currency_guid": 36,
            "operation_guid": 36,
            "project_guid": 36,
            "department_guid": 36,
            "analytics": 700,
        }
        for field, max_length in dimensions.items():
            value = source_data.get(field)
            if not isinstance(value, str) or len(value) > max_length:
                raise ValidationError("OData cash-flow audit dimensions are invalid.")
        for field in (
            "currency_guid", "operation_guid", "project_guid", "department_guid"
        ):
            if source_data[field]:
                normalize_guid(source_data[field], field=f"Snapshot {field}")
        if document != source_data["analytics"]:
            raise ValidationError("OData cash-flow display data does not match audit data.")
        if not isinstance(reference, str) or len(reference) > 300:
            raise ValidationError("OData cash-flow source reference is invalid.")
        receipts = _money(raw.get("receipts"), "receipts")
        payments = _money(raw.get("payments"), "payments")
        net = _money(raw.get("net_cash_flow"), "net_cash_flow")
        if receipts < 0 or payments < 0 or net != receipts - payments:
            raise ValidationError("OData cash-flow values are inconsistent.")
        records.append({
            "period_month": period,
            "source_row_number": line,
            "source_identity": expected_identity,
            "source_reference": reference,
            "article_raw": article,
            "normalized_article_name": raw["normalized_article_name"],
            "document_raw": document,
            "receipts": receipts,
            "payments": payments,
            "net_cash_flow": net,
            "source_data": source_data,
        })
    return records, scope


def _month_totals(rows):
    totals = defaultdict(lambda: {
        "row_count": 0,
        "receipts": Decimal("0.00"),
        "payments": Decimal("0.00"),
        "net_cash_flow": Decimal("0.00"),
    })
    for row in rows:
        period = row["period_month"]
        if isinstance(period, str):
            period = date.fromisoformat(period)
        item = totals[period]
        item["row_count"] += 1
        for field in ("receipts", "payments", "net_cash_flow"):
            item[field] += Decimal(row[field])
    return totals


def _preview_metadata(rows, warnings, organization, scope):
    draft = _month_totals(rows)
    active_states = {
        state.period_month: state.active_batch_id
        for state in OneCReportPeriodState.objects.filter(
            organization=organization,
            report_type=OneCImportBatch.TYPE_CASHFLOW,
            period_month__in=scope,
        )
    }
    active = _month_totals([
        {
            "period_month": row.period_month,
            "receipts": row.receipts,
            "payments": row.payments,
            "net_cash_flow": row.net_cash_flow,
        }
        for row in CashFlowRow.objects.active_for(
            organization, OneCImportBatch.TYPE_CASHFLOW
        ).filter(period_month__in=scope)
    ])
    monthly = []
    for month in scope:
        item = draft[month]
        has_active = month in active_states
        old = active[month] if has_active else None
        monthly.append({
            "month": month.strftime("%Y-%m"),
            "row_count": item["row_count"],
            "receipts": format(item["receipts"], "f"),
            "payments": format(item["payments"], "f"),
            "net_cash_flow": format(item["net_cash_flow"], "f"),
            "has_active": has_active,
            "active_receipts": format(old["receipts"], "f") if old else None,
            "active_payments": format(old["payments"], "f") if old else None,
            "active_net_cash_flow": format(old["net_cash_flow"], "f") if old else None,
            "receipts_difference": format(item["receipts"] - old["receipts"], "f") if old else None,
            "payments_difference": format(item["payments"] - old["payments"], "f") if old else None,
            "net_cash_flow_difference": format(item["net_cash_flow"] - old["net_cash_flow"], "f") if old else None,
        })
    articles = defaultdict(lambda: {
        "row_count": 0, "receipts": Decimal("0"),
        "payments": Decimal("0"), "net_cash_flow": Decimal("0"),
    })
    for row in rows:
        item = articles[row["article_raw"]]
        item["row_count"] += 1
        for field in ("receipts", "payments", "net_cash_flow"):
            item[field] += Decimal(row[field])
    total = {
        field: sum((draft[month][field] for month in scope), Decimal("0"))
        for field in ("receipts", "payments", "net_cash_flow")
    }
    return {
        "source": "odata",
        "scope_months": [month.isoformat() for month in scope],
        "report": {
            "layout": "odata_cashflow", "month_count": len(scope),
            "months": [month.isoformat() for month in scope],
        },
        "totals": {**{key: format(value, "f") for key, value in total.items()}, "row_count": len(rows)},
        "monthly": monthly,
        "articles": [
            {"article": name, **{
                key: format(value, "f") if isinstance(value, Decimal) else value
                for key, value in values.items()
            }} for name, values in sorted(articles.items())
        ],
        "overlap_months": [item["month"] for item in monthly if item["has_active"]],
        "overlap_count": sum(item["has_active"] for item in monthly),
        "warnings": warnings[:50],
        "warnings_total": len(warnings),
        "warnings_hidden": max(len(warnings) - 50, 0),
        "critical_errors": [],
        "preview": [
            {key: value for key, value in row.items() if key not in ("source_data", "source_identity")}
            for row in rows[:30]
        ],
    }


def _save_snapshot(batch, payload):
    content = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    batch.file_sha256 = hashlib.sha256(content).hexdigest()
    batch.file_size = len(content)
    batch.stored_file.save(batch.original_filename, ContentFile(content), save=False)
    try:
        batch.save()
    except Exception:
        delete_private_batch_file(batch)
        raise


def _failed_batch(start_month, end_month, scope, organization, user, message):
    return OneCImportBatch.objects.create(
        organization=organization,
        import_type=OneCImportBatch.TYPE_CASHFLOW,
        source_type=OneCImportBatch.SOURCE_ODATA,
        original_filename=f"onec-odata-cashflow-{start_month}-{end_month}.json",
        stored_file="",
        file_sha256=hashlib.sha256(
            f"failed:{organization.pk}:{start_month}:{end_month}:{message}:{uuid.uuid4()}".encode()
        ).hexdigest(),
        status=OneCImportBatch.STATUS_FAILED,
        uploaded_by=user,
        parser_version=PARSER_VERSION,
        period_first=scope[0], period_last=scope[-1],
        error_message=message[:ERROR_MESSAGE_MAX_LENGTH],
        metadata={
            "source": "odata", "scope_months": [m.isoformat() for m in scope],
            "report": {"layout": "odata_cashflow", "month_count": len(scope), "months": [m.isoformat() for m in scope]},
            "critical_errors": [message[:300]],
        },
    )


def create_odata_cashflow_draft(
    start_month, end_month, organization, user, *, config=None, opener=None
):
    _require_target(organization)
    scope = _month_scope(start_month, end_month)
    config = validate_config(config or config_from_settings())
    client = opener or build_opener(NoRedirectHandler())
    rows, page_count = read_cashflow_rows(
        config, start_month, end_month, opener=client
    )
    try:
        articles = _read_articles(
            config,
            {row.article_guid for row in rows if row.article_guid},
            opener=client,
            page_budget={"used": 0},
        )
        normalized, warnings = _normalise_rows(rows, articles)
        payload = {
            "schema": SNAPSHOT_SCHEMA,
            "start_month": start_month,
            "end_month": end_month,
            "scope_months": [month.isoformat() for month in scope],
            "organization_guids": list(config.organization_guids),
            "page_count": page_count,
            "rows": normalized,
        }
        _validate_snapshot(payload, config)
    except (ODataPreviewError, ValidationError) as exc:
        safe = str(exc)[:ERROR_MESSAGE_MAX_LENGTH]
        batch = _failed_batch(
            start_month, end_month, scope, organization, user, safe
        )
        raise ODataCashFlowDraftError(safe, batch=batch) from exc
    metadata = _preview_metadata(normalized, warnings, organization, scope)
    batch = OneCImportBatch(
        organization=organization,
        import_type=OneCImportBatch.TYPE_CASHFLOW,
        source_type=OneCImportBatch.SOURCE_ODATA,
        original_filename=f"onec-odata-cashflow-{start_month}-{end_month}.json",
        status=OneCImportBatch.STATUS_PREVIEWED,
        uploaded_by=user,
        parser_version=PARSER_VERSION,
        rows_detected=len(normalized),
        warnings_count=len(warnings),
        period_first=scope[0], period_last=scope[-1],
        metadata=metadata,
    )
    try:
        _save_snapshot(batch, payload)
    except IntegrityError as exc:
        raise ODataCashFlowDraftError(
            "An identical OData cash-flow snapshot already exists"
        ) from exc
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
        raise ValidationError("OData cash-flow snapshot checksum has changed.")
    try:
        return json.loads(bytes(content).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("OData cash-flow snapshot JSON is invalid.") from exc


def confirm_odata_cashflow(batch_id, organization, user, *, config=None):
    _require_target(organization)
    config = validate_config(config or config_from_settings())
    try:
        with transaction.atomic():
            locked_organization = Organization.objects.select_for_update().get(
                pk=organization.pk
            )
            batch = OneCImportBatch.objects.select_for_update().get(
                id=batch_id,
                organization=locked_organization,
                import_type=OneCImportBatch.TYPE_CASHFLOW,
                source_type=OneCImportBatch.SOURCE_ODATA,
            )
            if batch.status != OneCImportBatch.STATUS_PREVIEWED:
                raise ValidationError("Only a previewed OData cash-flow draft can be confirmed.")
            if batch.parser_version != PARSER_VERSION:
                raise ValidationError("OData cash-flow draft version is no longer supported.")
            payload = _read_snapshot(batch)
            records, periods = _validate_snapshot(payload, config)
            if batch.period_first != periods[0] or batch.period_last != periods[-1]:
                raise ValidationError("OData cash-flow draft period does not match its snapshot.")
            if batch.rows_detected != len(records):
                raise ValidationError("OData cash-flow draft row count does not match its snapshot.")
            if (batch.metadata or {}).get("scope_months") != payload["scope_months"]:
                raise ValidationError("OData cash-flow draft metadata scope does not match its snapshot.")
            locked_states = list(
                OneCReportPeriodState.objects.select_for_update()
                .filter(
                    organization=locked_organization,
                    report_type=OneCImportBatch.TYPE_CASHFLOW,
                    period_month__in=periods,
                )
                .select_related("active_batch")
                .order_by("period_month")
            )
            rows = [
                CashFlowRow(
                    import_batch=batch,
                    organization=locked_organization,
                    **record,
                ) for record in records
            ]
            CashFlowRow.objects.bulk_create(rows, batch_size=500)
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
        safe = (
            f"OData cash-flow draft confirmation failed: {type(exc).__name__}."
        )[:ERROR_MESSAGE_MAX_LENGTH]
        updated = OneCImportBatch.objects.filter(
            id=batch_id,
            organization=organization,
            import_type=OneCImportBatch.TYPE_CASHFLOW,
            source_type=OneCImportBatch.SOURCE_ODATA,
            status=OneCImportBatch.STATUS_PREVIEWED,
        ).update(status=OneCImportBatch.STATUS_FAILED, error_message=safe)
        if updated:
            failed_batch = OneCImportBatch.objects.get(
                id=batch_id, organization=organization
            )
            _audit(
                failed_batch,
                user,
                {"status": OneCImportBatch.STATUS_PREVIEWED},
                {"status": OneCImportBatch.STATUS_FAILED},
            )
        raise
