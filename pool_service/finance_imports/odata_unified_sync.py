"""Resumable, preview-only smart synchronization for 1C OData finance data."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
import logging
import uuid
from urllib.request import build_opener

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from pool_service.models import (
    CashFlowRow,
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCODataSyncRun,
    OneCReportPeriodState,
    Organization,
)
from pool_service.services.finance import can_import_cashflow, can_import_gross_profit
from pool_service.services.permissions import company_has_access
from .odata_cashflow import read_cashflow_rows
from .odata_cashflow_drafts import (
    PARSER_VERSION as CASHFLOW_PARSER_VERSION,
    SNAPSHOT_SCHEMA as CASHFLOW_SCHEMA,
    _audit as cashflow_audit,
    _normalise_rows as normalise_cashflow_rows,
    _preview_metadata as cashflow_preview_metadata,
    _read_articles,
    _save_snapshot as save_cashflow_snapshot,
    _validate_snapshot as validate_cashflow_snapshot,
)
from .odata_profit import NoRedirectHandler, ODataPreviewError, read_profit_rows, validate_config
from .odata_profit_drafts import (
    PARSER_VERSION as PROFIT_PARSER_VERSION,
    SNAPSHOT_SCHEMA as PROFIT_SCHEMA,
    _audit as profit_audit,
    _enrich_rows,
    _preview_metadata as profit_preview_metadata,
    _read_reference_map,
    _read_snapshot as read_profit_snapshot,
    _save_batch_snapshot,
    _bulk_create_monthly_rows,
    _save_confirmed_batch,
    _validate_snapshot as validate_profit_snapshot,
    config_from_settings,
    is_odata_target_organization,
)
from .odata_cashflow_drafts import _read_snapshot as read_cashflow_snapshot
from .services import _activate_period_states, validate_period_assignment
from .validators import delete_private_batch_file


REPORT_PROFIT = OneCImportBatch.TYPE_MONTHLY_PROFIT
REPORT_CASHFLOW = OneCImportBatch.TYPE_CASHFLOW
SUPPORTED_REPORT_TYPES = (REPORT_PROFIT, REPORT_CASHFLOW)
CHUNK_MONTHS = 12
INITIAL_MONTHS = 12
LEASE_SECONDS = 300
SAFE_ERROR_MESSAGE = "Не удалось проверить данные 1С. Продолжите проверку позже."
logger = logging.getLogger(__name__)

# These values are persisted in JSON and may be exposed to an authorised user.
# Keep the diagnostic vocabulary deliberately small and free of transport details.
STAGE_CONFIG = "config"
STAGE_PROFIT_READ = "profit_read"
STAGE_PROFIT_REFERENCE_GUID_VALIDATION = "profit_reference_guid_validation"
STAGE_PROFIT_NOMENCLATURE_LOOKUP = "profit_nomenclature_lookup"
STAGE_PROFIT_CUSTOMER_LOOKUP = "profit_customer_lookup"
STAGE_PROFIT_RESPONSIBLE_LOOKUP = "profit_responsible_lookup"
STAGE_PROFIT_NORMALIZATION = "profit_normalization"
STAGE_CASHFLOW_READ = "cashflow_read"
STAGE_CASHFLOW_REFERENCE_LOOKUP = "cashflow_reference_lookup"
STAGE_CASHFLOW_NORMALIZATION = "cashflow_normalization"
STEP_ERROR_CODES = {
    STAGE_CONFIG: "sync_config_failed",
    STAGE_PROFIT_READ: "profit_read_failed",
    STAGE_PROFIT_REFERENCE_GUID_VALIDATION: "profit_reference_guid_validation_failed",
    STAGE_PROFIT_NOMENCLATURE_LOOKUP: "profit_nomenclature_lookup_failed",
    STAGE_PROFIT_CUSTOMER_LOOKUP: "profit_customer_lookup_failed",
    STAGE_PROFIT_RESPONSIBLE_LOOKUP: "profit_responsible_lookup_failed",
    STAGE_PROFIT_NORMALIZATION: "profit_normalization_failed",
    STAGE_CASHFLOW_READ: "cashflow_read_failed",
    STAGE_CASHFLOW_REFERENCE_LOOKUP: "cashflow_reference_lookup_failed",
    STAGE_CASHFLOW_NORMALIZATION: "cashflow_normalization_failed",
}
PROFIT_NOMENCLATURE_ERROR_REASONS = frozenset({
    "http_error",
    "request_failed",
    "page_limit",
    "unexpected_rows",
    "invalid_reference_row",
    "deleted_reference",
    "missing_description",
    "invalid_nomenclature_type",
    "reference_missing",
    "unexpected",
})
PROFIT_CUSTOMER_ERROR_REASONS = frozenset({
    "http_error",
    "request_failed",
    "page_limit",
    "unexpected_rows",
    "invalid_reference_row",
    "deleted_reference",
    "missing_description",
    "reference_missing",
    "unexpected",
})
STAGE_ERROR_REASONS = {
    STAGE_PROFIT_NOMENCLATURE_LOOKUP: PROFIT_NOMENCLATURE_ERROR_REASONS,
    STAGE_PROFIT_CUSTOMER_LOOKUP: PROFIT_CUSTOMER_ERROR_REASONS,
}


class UnifiedSyncError(ValidationError):
    pass


class StaleReactivationError(UnifiedSyncError):
    pass


class SyncConflictError(UnifiedSyncError):
    pass


class UnifiedSyncStageError(Exception):
    """Internal wrapper that carries only an allowlisted collection stage."""

    def __init__(self, stage, *, error_reason=None):
        self.stage = stage
        self.error_reason = error_reason
        super().__init__(stage)


def _raise_stage_error(stage, exc, *, error_reason=None):
    raise UnifiedSyncStageError(stage, error_reason=error_reason) from exc


def _profit_nomenclature_error_reason(exc):
    """Return a persisted-safe reason only for controlled reader errors."""
    if not isinstance(exc, ODataPreviewError):
        return None
    message = str(exc)
    if message.startswith("OData HTTP error "):
        return "http_error"
    if message.startswith("OData request failed"):
        return "request_failed"
    if message in {
        "OData pagination exceeded the configured page limit",
        "1C reference lookups exceeded the page limit",
    }:
        return "page_limit"
    if message.startswith("1C reference lookup returned unexpected"):
        return "unexpected_rows"
    if (
        message.startswith("1C reference row must be an object")
        or message.startswith("Ref_Key ")
        or message.startswith("1C nomenclature article must be a string")
    ):
        return "invalid_reference_row"
    if message.startswith("1C reference is deleted or has an invalid deletion mark"):
        return "deleted_reference"
    if message.startswith("1C reference has no display description") or message.startswith(
        "1C reference description must not be a GUID"
    ):
        return "missing_description"
    if message.startswith("1C nomenclature type is invalid"):
        return "invalid_nomenclature_type"
    if message.startswith("1C reference is missing or unavailable"):
        return "reference_missing"
    return "unexpected"


def _profit_customer_error_reason(exc):
    """Return a persisted-safe reason only for controlled customer lookup errors."""
    if not isinstance(exc, ODataPreviewError):
        return None
    message = str(exc)
    if message.startswith("OData HTTP error "):
        return "http_error"
    if message.startswith("OData request failed"):
        return "request_failed"
    if message in {
        "OData pagination exceeded the configured page limit",
        "1C reference lookups exceeded the page limit",
    }:
        return "page_limit"
    if message.startswith("1C reference lookup returned unexpected"):
        return "unexpected_rows"
    if message.startswith("1C reference row must be an object") or message.startswith("Ref_Key "):
        return "invalid_reference_row"
    if message.startswith("1C reference is deleted or has an invalid deletion mark"):
        return "deleted_reference"
    if message.startswith("1C reference has no display description") or message.startswith(
        "1C reference description must not be a GUID"
    ):
        return "missing_description"
    if message.startswith("1C reference is missing or unavailable"):
        return "reference_missing"
    return "unexpected"


def _log_step_failure(stage, correlation_id, exc):
    """Write only allowlisted diagnostic fields to the dedicated log."""
    logger.error(
        "Unified 1C sync step failed correlation_id=%s stage=%s exception_class=%s",
        correlation_id,
        stage,
        type(exc).__name__,
    )


def _has_report_permission(user, organization, report_type):
    if not company_has_access(organization):
        return False
    permission = (
        can_import_gross_profit
        if report_type == REPORT_PROFIT
        else can_import_cashflow
        if report_type == REPORT_CASHFLOW
        else None
    )
    return bool(permission and permission(user, organization))


def _next_month(value):
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def _add_months(value, offset):
    index = value.year * 12 + value.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def _months(start, end):
    result = []
    current = start
    while current <= end:
        result.append(current)
        current = _next_month(current)
    return result


def _scope_for(organization, report_type, today):
    earliest = (
        OneCReportPeriodState.objects.filter(
            organization=organization, report_type=report_type,
            active_batch__status=OneCImportBatch.STATUS_CONFIRMED,
        )
        .order_by("period_month")
        .values_list("period_month", flat=True)
        .first()
    )
    initial = earliest is None
    start = earliest or _add_months(today, -(INITIAL_MONTHS - 1))
    return start, today, initial


def _chunk_scope(start, end):
    months = _months(start, end)
    return [
        {"start": part[0].isoformat(), "end": part[-1].isoformat()}
        for index in range(0, len(months), CHUNK_MONTHS)
        if (part := months[index:index + CHUNK_MONTHS])
    ]


def _scope_fingerprint(organization_id, report_types, scopes):
    payload = {
        "organization_id": organization_id,
        "source": OneCImportBatch.SOURCE_ODATA,
        "report_types": list(report_types),
        "scopes": scopes,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def start_unified_sync(
    organization, user, report_types, *, today=None,
    mode=OneCODataSyncRun.MODE_PREVIEW, period_start=None, period_end=None,
):
    requested = [item for item in SUPPORTED_REPORT_TYPES if item in set(report_types)]
    if not requested:
        raise PermissionDenied("No permitted 1C report types were selected.")
    if not is_odata_target_organization(organization):
        raise UnifiedSyncError("OData import is not configured for this organization")
    if mode not in {OneCODataSyncRun.MODE_PREVIEW, OneCODataSyncRun.MODE_AUTO_APPLY}:
        raise UnifiedSyncError("Unknown synchronization mode")
    today = (today or timezone.localdate()).replace(day=1)
    if mode == OneCODataSyncRun.MODE_AUTO_APPLY:
        if not period_start or not period_end:
            period_end = today
            period_start = _add_months(period_end, -(INITIAL_MONTHS - 1))
        if period_start.day != 1 or period_end.day != 1 or period_start > period_end:
            raise UnifiedSyncError("Период должен состоять из полных календарных месяцев.")
        if len(_months(period_start, period_end)) > 24:
            raise UnifiedSyncError("Период не может превышать 24 месяца.")
    with transaction.atomic():
        locked = Organization.objects.select_for_update().get(pk=organization.pk)
        scopes = {}
        queue = []
        for report_type in requested:
            if mode == OneCODataSyncRun.MODE_AUTO_APPLY:
                start, end, initial = period_start, period_end, False
            else:
                start, end, initial = _scope_for(locked, report_type, today)
            chunks = _chunk_scope(start, end)
            scopes[report_type] = {
                "start": start.isoformat(), "end": end.isoformat(),
                "initial_import": initial, "chunks": chunks,
            }
            queue.extend({"report_type": report_type, **chunk} for chunk in chunks)
        fingerprint = _scope_fingerprint(locked.pk, requested, scopes)
        existing = OneCODataSyncRun.objects.filter(
            organization=locked,
            status__in=[OneCODataSyncRun.STATUS_PENDING, OneCODataSyncRun.STATUS_RUNNING],
        ).first()
        if existing:
            existing_fingerprint = (existing.sync_scope or {}).get("_scope_fingerprint")
            if existing.mode == mode and (
                mode == OneCODataSyncRun.MODE_PREVIEW or existing_fingerprint == fingerprint
            ):
                return existing, False
            raise SyncConflictError("Another synchronization is already running")
        if mode == OneCODataSyncRun.MODE_AUTO_APPLY:
            scopes["_scope_fingerprint"] = fingerprint
            scopes["_baseline"] = []
            scopes["_apply_plan"] = []
        summary = {
            report_type: {"status": "pending", "changed_months": [], "unchanged_months": [], "drafts": [], "reactivation_candidates": [], "error_code": "", "error": ""}
            for report_type in requested
        }
        run = OneCODataSyncRun.objects.create(
            organization=locked,
            requested_by=user,
            mode=mode,
            idempotency_key=(uuid.uuid4().hex if mode == OneCODataSyncRun.MODE_AUTO_APPLY else None),
            requested_report_types=requested,
            sync_scope=scopes,
            cursor={"index": 0, "version": 0, "queue": queue},
            progress={"completed_chunks": 0, "total_chunks": len(queue)},
            result_summary=summary,
        )
    return run, True


def _decimal(value):
    if value is None:
        return None
    normalized = Decimal(value).normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _canonical_profit(row):
    get = row.get if isinstance(row, dict) else lambda key, default=None: getattr(row, key, default)
    return {
        "source_identity": get("source_identity"),
        "period_month": get("period_month").isoformat() if isinstance(get("period_month"), date) else get("period_month"),
        "manager_name": get("manager_name", ""), "customer_name": get("customer_name", ""),
        "document_name": get("document_name", ""), "nomenclature": get("nomenclature", ""),
        "article": get("article", ""), "nomenclature_type": get("nomenclature_type", ""),
        "quantity": _decimal(get("quantity")), "revenue": _decimal(get("revenue")),
        "cost": _decimal(get("cost")), "gross_profit": _decimal(get("gross_profit")),
        "calculated_cost": _decimal(get("calculated_cost")), "cost_source": get("cost_source", ""),
        "cost_calculation_method": get("cost_calculation_method", ""),
        "cost_calculation_ratio": _decimal(get("cost_calculation_ratio")),
        "analytical_gross_profit": _decimal(get("analytical_gross_profit")),
        "profitability_percent": _decimal(get("profitability_percent")),
    }


def _canonical_cashflow(row):
    get = row.get if isinstance(row, dict) else lambda key, default=None: getattr(row, key, default)
    return {
        "source_identity": get("source_identity"),
        "period_month": get("period_month").isoformat() if isinstance(get("period_month"), date) else get("period_month"),
        "source_reference": get("source_reference", ""), "article_raw": get("article_raw", ""),
        "normalized_article_name": get("normalized_article_name", ""),
        "document_raw": get("document_raw", ""), "receipts": _decimal(get("receipts")),
        "payments": _decimal(get("payments")), "net_cash_flow": _decimal(get("net_cash_flow")),
    }


def month_fingerprint(report_type, month, rows):
    canonical = _canonical_profit if report_type == REPORT_PROFIT else _canonical_cashflow
    payload = {
        "report_type": report_type,
        "month": month.isoformat(),
        "rows": sorted((canonical(row) for row in rows), key=lambda item: item["source_identity"]),
    }
    content = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(content).hexdigest()


def _active_rows(organization, report_type, month):
    if report_type == REPORT_PROFIT:
        return list(OneCMonthlyProfit.objects.active_for(organization).filter(period_month=month))
    return list(CashFlowRow.objects.active_for(organization, REPORT_CASHFLOW).filter(period_month=month))


def _profit_reference_guid(value, *, allow_zero=False):
    normalized = str(uuid.UUID(str(value))).lower()
    if normalized == "00000000-0000-0000-0000-000000000000":
        if allow_zero:
            return None
        raise ValueError("A required OData reference is empty")
    return normalized


def _collect_profit_chunk(start, end, *, config, opener):
    try:
        rows, pages = read_profit_rows(config, start[:7], end[:7], opener=opener)
    except Exception as exc:
        _raise_stage_error(STAGE_PROFIT_READ, exc)
    try:
        required = {
            "nomenclature": {_profit_reference_guid(row.nomenclature_guid) for row in rows},
            "customer": {
                guid for row in rows
                if (guid := _profit_reference_guid(row.customer_guid, allow_zero=True))
            },
            "responsible": {_profit_reference_guid(row.responsible_guid) for row in rows},
        }
    except Exception as exc:
        _raise_stage_error(STAGE_PROFIT_REFERENCE_GUID_VALIDATION, exc)

    budget = {"used": 0}
    references = {}
    for kind, stage in (
        ("nomenclature", STAGE_PROFIT_NOMENCLATURE_LOOKUP),
        ("customer", STAGE_PROFIT_CUSTOMER_LOOKUP),
        ("responsible", STAGE_PROFIT_RESPONSIBLE_LOOKUP),
    ):
        try:
            lookup_kwargs = {"opener": opener, "page_budget": budget}
            if kind == "nomenclature":
                lookup_kwargs["allow_deleted_nomenclature"] = True
            elif kind == "customer":
                lookup_kwargs["allow_deleted_customer"] = True
            references[kind] = _read_reference_map(
                config,
                kind,
                required[kind],
                **lookup_kwargs,
            )
        except Exception as exc:
            error_reason = None
            if stage == STAGE_PROFIT_NOMENCLATURE_LOOKUP:
                error_reason = _profit_nomenclature_error_reason(exc)
            elif stage == STAGE_PROFIT_CUSTOMER_LOOKUP:
                error_reason = _profit_customer_error_reason(exc)
            _raise_stage_error(
                stage,
                exc,
                error_reason=error_reason,
            )
    try:
        return _enrich_rows(rows, references), pages
    except Exception as exc:
        _raise_stage_error(STAGE_PROFIT_NORMALIZATION, exc)


def _collect_cashflow_chunk(start, end, *, config, opener):
    try:
        rows, pages = read_cashflow_rows(config, start[:7], end[:7], opener=opener)
    except Exception as exc:
        _raise_stage_error(STAGE_CASHFLOW_READ, exc)
    try:
        articles = _read_articles(
            config, {row.article_guid for row in rows if row.article_guid},
            opener=opener, page_budget={"used": 0},
        )
    except Exception as exc:
        _raise_stage_error(STAGE_CASHFLOW_REFERENCE_LOOKUP, exc)
    try:
        normalized, warnings = normalise_cashflow_rows(rows, articles)
    except Exception as exc:
        _raise_stage_error(STAGE_CASHFLOW_NORMALIZATION, exc)
    return normalized, pages, warnings


def _existing_preview(organization, report_type, month, fingerprint):
    candidates = OneCImportBatch.objects.filter(
        organization=organization, import_type=report_type,
        source_type=OneCImportBatch.SOURCE_ODATA,
        status=OneCImportBatch.STATUS_PREVIEWED,
        period_first=month, period_last=month,
        sync_run__isnull=True,
    )
    for batch in candidates:
        stored = (batch.metadata or {}).get("month_fingerprint")
        if stored == fingerprint:
            return batch
        if stored:
            continue
        try:
            payload = (
                read_profit_snapshot(batch)
                if report_type == REPORT_PROFIT
                else read_cashflow_snapshot(batch)
            )
            rows = payload.get("rows", [])
            if month_fingerprint(report_type, month, rows) == fingerprint:
                return batch
        except (OSError, ValueError, ValidationError):
            continue
    return None


def _confirmed_candidate(organization, report_type, month, fingerprint):
    candidates = OneCImportBatch.objects.filter(
        organization=organization,
        import_type=report_type,
        source_type=OneCImportBatch.SOURCE_ODATA,
        status=OneCImportBatch.STATUS_CONFIRMED,
        sync_run__isnull=True,
        period_first__lte=month,
        period_last__gte=month,
    ).exclude(
        active_period_states__organization=organization,
        active_period_states__report_type=report_type,
        active_period_states__period_month=month,
    )
    for batch in candidates:
        model = OneCMonthlyProfit if report_type == REPORT_PROFIT else CashFlowRow
        rows = model.objects.filter(import_batch=batch, period_month=month)
        try:
            validate_period_assignment(batch, organization, report_type, month)
        except ValidationError:
            continue
        if month_fingerprint(report_type, month, rows) == fingerprint:
            return batch
    return None


def _create_month_draft(run, report_type, month, rows, pages, config, warnings):
    fingerprint = month_fingerprint(report_type, month, rows)
    existing = None if run.mode == OneCODataSyncRun.MODE_AUTO_APPLY else _existing_preview(
        run.organization, report_type, month, fingerprint
    )
    if existing:
        return existing, fingerprint, False
    month_text = month.strftime("%Y-%m")
    payload = {
        "schema": PROFIT_SCHEMA if report_type == REPORT_PROFIT else CASHFLOW_SCHEMA,
        "start_month": month_text, "end_month": month_text,
        "scope_months": [month.isoformat()],
        "organization_guids": list(config.organization_guids),
        "page_count": max(1, pages), "rows": rows,
    }
    if report_type == REPORT_PROFIT:
        validate_profit_snapshot(payload, config)
        metadata = profit_preview_metadata(rows, run.organization, [month])
        filename = f"onec-odata-{month_text}-{month_text}.json"
        parser = PROFIT_PARSER_VERSION
    else:
        validate_cashflow_snapshot(payload, config)
        metadata = cashflow_preview_metadata(rows, warnings, run.organization, [month])
        filename = f"onec-odata-cashflow-{month_text}-{month_text}.json"
        parser = CASHFLOW_PARSER_VERSION
    metadata.update({"month_fingerprint": fingerprint, "automatically_detected_change": True, "sync_run_id": str(run.id)})
    batch = OneCImportBatch(
        organization=run.organization, import_type=report_type,
        source_type=OneCImportBatch.SOURCE_ODATA, original_filename=filename,
        status=OneCImportBatch.STATUS_PREVIEWED, uploaded_by=run.requested_by,
        parser_version=parser, rows_detected=len(rows), period_first=month,
        period_last=month, metadata=metadata,
        sync_run=(run if run.mode == OneCODataSyncRun.MODE_AUTO_APPLY else None),
        warnings_count=len(warnings) if report_type == REPORT_CASHFLOW else 0,
    )
    try:
        if report_type == REPORT_PROFIT:
            _save_batch_snapshot(batch, payload)
            profit_audit(batch, run.requested_by, {"status": "uploaded"}, {"status": "previewed"})
        else:
            save_cashflow_snapshot(batch, payload)
            cashflow_audit(batch, run.requested_by, {"status": "uploaded"}, {"status": "previewed"})
    except Exception:
        delete_private_batch_file(batch)
        raise
    return batch, fingerprint, True


def _terminal_status(summary):
    states = [item["status"] for item in summary.values()]
    if all(state == "completed" for state in states):
        return OneCODataSyncRun.STATUS_COMPLETED
    if any(state == "completed" for state in states):
        return OneCODataSyncRun.STATUS_PARTIAL_FAILED
    return OneCODataSyncRun.STATUS_FAILED


def _auto_apply_fault(_stage):
    """Test seam for proving rollback at every mutation boundary."""


def _prepared_auto_candidates(run, config):
    prepared = []
    for item in (run.sync_scope or {}).get("_apply_plan", []):
        batch = OneCImportBatch.objects.get(
            pk=item["batch_id"], sync_run=run,
            status=OneCImportBatch.STATUS_PREVIEWED,
        )
        if batch.import_type == REPORT_PROFIT:
            payload = read_profit_snapshot(batch)
            records, periods = validate_profit_snapshot(payload, config)
        elif batch.import_type == REPORT_CASHFLOW:
            payload = read_cashflow_snapshot(batch)
            records, periods = validate_cashflow_snapshot(payload, config)
        else:
            raise UnifiedSyncError("Unsupported auto-apply candidate")
        if periods != [date.fromisoformat(item["month"])] or batch.rows_detected != len(records):
            raise UnifiedSyncError("Auto-apply candidate scope changed")
        if month_fingerprint(batch.import_type, periods[0], records) != item.get("fingerprint"):
            raise UnifiedSyncError("Auto-apply candidate fingerprint changed")
        prepared.append((batch.pk, batch.import_type, periods, records))
    return prepared


def _expected_auto_scope_keys(run):
    expected = set()
    for report_type in run.requested_report_types:
        scope = (run.sync_scope or {}).get(report_type)
        if report_type not in SUPPORTED_REPORT_TYPES or not isinstance(scope, dict):
            raise UnifiedSyncError("Auto-apply scope is incomplete")
        start = date.fromisoformat(scope["start"])
        end = date.fromisoformat(scope["end"])
        expected.update((report_type, month) for month in _months(start, end))
    if not expected:
        raise UnifiedSyncError("Auto-apply scope is empty")
    return expected


def _validate_auto_collection(run):
    cursor = run.cursor or {}
    queue = cursor.get("queue", [])
    if (
        run.status != OneCODataSyncRun.STATUS_COMPLETED
        or int(cursor.get("index", -1)) != len(queue)
        or not queue
        or any(
            (run.result_summary or {}).get(report_type, {}).get("status") != "completed"
            for report_type in run.requested_report_types
        )
    ):
        raise UnifiedSyncError("Auto-apply collection is not complete")
    expected_keys = _expected_auto_scope_keys(run)
    baseline = list((run.sync_scope or {}).get("_baseline", []))
    baseline_keys = [
        (item.get("report_type"), date.fromisoformat(item["month"]))
        for item in baseline
    ]
    if len(baseline_keys) != len(set(baseline_keys)) or set(baseline_keys) != expected_keys:
        raise UnifiedSyncError("Auto-apply baseline does not cover the full scope")
    allowed_source_statuses = {"unchanged", "changed", "authoritative_empty"}
    if any(item.get("source_status") not in allowed_source_statuses for item in baseline):
        raise UnifiedSyncError("Auto-apply baseline has an invalid source status")
    expected_apply_plan_keys = {
        (item["report_type"], date.fromisoformat(item["month"]))
        for item in baseline
        if item["source_status"] in {"changed", "authoritative_empty"}
    }
    plan = list((run.sync_scope or {}).get("_apply_plan", []))
    plan_keys = [
        (item.get("report_type"), date.fromisoformat(item["month"]))
        for item in plan
    ]
    if (
        len(plan_keys) != len(set(plan_keys))
        or not set(plan_keys).issubset(expected_keys)
        or set(plan_keys) != expected_apply_plan_keys
    ):
        raise UnifiedSyncError("Auto-apply plan does not match the collected changes")
    return baseline, plan


def _fail_auto_run(run_id, code, message):
    with transaction.atomic():
        run = OneCODataSyncRun.objects.select_for_update().get(pk=run_id)
        if run.mode != OneCODataSyncRun.MODE_AUTO_APPLY:
            raise OneCODataSyncRun.DoesNotExist
        run.status = OneCODataSyncRun.STATUS_FAILED
        run.finished_at = timezone.now()
        run.error_message = message
        progress = dict(run.progress)
        progress.update({"step_state": code, "outcome": "failed"})
        run.progress = progress
        _clear_lease(run)
        run.save()
        return run


def _auto_run_already_succeeded(run):
    return bool(
        run.applied_at
        or (run.progress or {}).get("outcome") in {"applied", "no_change"}
    )


def apply_auto_sync(run_id, user, allowed_report_types, *, config=None):
    run = OneCODataSyncRun.objects.get(pk=run_id, mode=OneCODataSyncRun.MODE_AUTO_APPLY)
    if _auto_run_already_succeeded(run):
        return run
    config = validate_config(config or config_from_settings())
    try:
        _validate_auto_collection(run)
        prepared = _prepared_auto_candidates(run, config)
    except Exception:
        return _fail_auto_run(run_id, "candidate_invalid", "Не удалось проверить собранные данные 1С.")
    try:
        with transaction.atomic():
            locked = OneCODataSyncRun.objects.select_for_update().select_related("organization").get(
                pk=run_id, mode=OneCODataSyncRun.MODE_AUTO_APPLY
            )
            if _auto_run_already_succeeded(locked):
                return locked
            baseline, apply_plan = _validate_auto_collection(locked)
            organization = Organization.objects.select_for_update().get(pk=locked.organization_id)
            requested = set(locked.requested_report_types)
            allowed = set(allowed_report_types)
            if not requested or not requested.issubset(allowed) or any(
                not _has_report_permission(user, organization, item) for item in requested
            ):
                raise PermissionDenied
            keys = {(item["report_type"], date.fromisoformat(item["month"])) for item in baseline}
            states = list(
                OneCReportPeriodState.objects.select_for_update()
                .filter(
                    organization=organization,
                    report_type__in=requested,
                    period_month__in={month for _, month in keys},
                )
                .select_related("active_batch")
            )
            state_map = {(state.report_type, state.period_month): state for state in states}
            for expected in baseline:
                key = (expected["report_type"], date.fromisoformat(expected["month"]))
                current = state_map.get(key)
                current_id = str(current.active_batch_id) if current else None
                current_fp = month_fingerprint(
                    expected["report_type"], key[1],
                    _active_rows(organization, expected["report_type"], key[1]) if current else [],
                )
                if current_id != expected["expected_active_batch_id"] or current_fp != expected["expected_active_fingerprint"]:
                    raise StaleReactivationError("Active scope changed")
            locked.apply_started_at = timezone.now()
            locked.save(update_fields=["apply_started_at"])
            _auto_apply_fault("before_rows")
            prepared_by_id = {str(batch_id): (report_type, periods, records) for batch_id, report_type, periods, records in prepared}
            for item in apply_plan:
                batch_id = item["batch_id"]
                batch = OneCImportBatch.objects.select_for_update().get(
                    pk=batch_id, sync_run=locked, status=OneCImportBatch.STATUS_PREVIEWED,
                )
                if str(batch.pk) not in prepared_by_id or batch.import_type != item["report_type"]:
                    raise UnifiedSyncError("Auto-apply candidate set changed")
                if batch.import_type == REPORT_PROFIT:
                    payload = read_profit_snapshot(batch)
                    records, periods = validate_profit_snapshot(payload, config)
                else:
                    payload = read_cashflow_snapshot(batch)
                    records, periods = validate_cashflow_snapshot(payload, config)
                expected_month = date.fromisoformat(item["month"])
                if (
                    periods != [expected_month]
                    or batch.rows_detected != len(records)
                    or month_fingerprint(batch.import_type, expected_month, records) != item.get("fingerprint")
                ):
                    raise UnifiedSyncError("Auto-apply candidate changed after collection")
                report_type = batch.import_type
                if report_type == REPORT_PROFIT:
                    rows = [OneCMonthlyProfit(import_batch=batch, organization=organization, **record) for record in records]
                    _bulk_create_monthly_rows(rows)
                    _auto_apply_fault("profit_rows")
                    profit_audit(batch, user, {"status": "previewed"}, {"status": "confirmed"})
                else:
                    rows = [CashFlowRow(import_batch=batch, organization=organization, **record) for record in records]
                    CashFlowRow.objects.bulk_create(rows, batch_size=500)
                    _auto_apply_fault("cashflow_rows")
                    cashflow_audit(batch, user, {"status": "previewed"}, {"status": "confirmed"})
                batch.status = OneCImportBatch.STATUS_CONFIRMED
                _save_confirmed_batch(batch, user, len(rows))
                _auto_apply_fault("confirmed_batch")
                relevant = [state_map[(report_type, month)] for month in periods if (report_type, month) in state_map]
                _activate_period_states(batch, organization, user, periods, relevant)
                _auto_apply_fault("state_activation")
            _auto_apply_fault("before_success")
            locked.status = OneCODataSyncRun.STATUS_COMPLETED
            locked.applied_at = timezone.now()
            locked.finished_at = locked.applied_at
            locked.error_message = ""
            progress = dict(locked.progress)
            progress.update({
                "step_state": "completed",
                "outcome": "applied" if prepared else "no_change",
                "applied_batches": len(prepared),
            })
            locked.progress = progress
            locked.save()
            return locked
    except StaleReactivationError:
        return _fail_auto_run(run_id, "stale", "Активные данные изменились. Запустите обновление повторно.")
    except PermissionDenied:
        return _fail_auto_run(run_id, "permission_revoked", "Право на обновление данных было отозвано.")
    except Exception:
        return _fail_auto_run(run_id, "apply_failed", "Не удалось применить данные 1С.")


def _clear_lease(run):
    run.lease_token = None
    run.lease_report_type = ""
    run.lease_chunk = {}
    run.lease_started_at = None


def _claim_step(
    run_id, user, allowed, expected_cursor,
    expected_mode=OneCODataSyncRun.MODE_PREVIEW,
):
    now = timezone.now()
    with transaction.atomic():
        run = OneCODataSyncRun.objects.select_for_update().select_related("organization").get(
            pk=run_id, mode=expected_mode
        )
        if run.status in OneCODataSyncRun.TERMINAL_STATUSES:
            return run, None
        cursor = dict(run.cursor)
        if int(expected_cursor) != int(cursor.get("version", 0)):
            return run, None
        if run.lease_token and run.lease_started_at and run.lease_started_at > now - timedelta(seconds=LEASE_SECONDS):
            progress = dict(run.progress)
            progress["step_state"] = "busy"
            run.progress = progress
            return run, None
        queue = cursor.get("queue", [])
        index = int(cursor.get("index", 0))
        if index >= len(queue):
            run.status = _terminal_status(run.result_summary)
            run.finished_at = now
            _clear_lease(run)
            run.save()
            return run, None
        item = queue[index]
        if item["report_type"] not in allowed or not _has_report_permission(
            user, run.organization, item["report_type"]
        ):
            raise PermissionDenied("Permission for this 1C report type is required.")
        token = uuid.uuid4()
        run.lease_token = token
        run.lease_report_type = item["report_type"]
        run.lease_chunk = {**item, "cursor_version": int(expected_cursor)}
        run.lease_started_at = now
        if run.status == OneCODataSyncRun.STATUS_PENDING:
            run.status = OneCODataSyncRun.STATUS_RUNNING
            run.started_at = now
        progress = dict(run.progress)
        progress["step_state"] = "running"
        run.progress = progress
        run.save(update_fields=[
            "lease_token", "lease_report_type", "lease_chunk", "lease_started_at",
            "status", "started_at", "progress",
        ])
        return run, token


def _safe_step_failure(run_id, token, expected_cursor, stage, exc, *, error_reason=None):
    """Persist a retryable failure without retaining transport or payload data."""
    if stage not in STEP_ERROR_CODES:
        stage = STAGE_CONFIG
    correlation_id = uuid.uuid4().hex
    _log_step_failure(stage, correlation_id, exc)
    with transaction.atomic():
        run = OneCODataSyncRun.objects.select_for_update().get(pk=run_id)
        if str(run.lease_token or "") != str(token) or int(run.cursor.get("version", 0)) != int(expected_cursor):
            return run
        summary = dict(run.result_summary)
        report_type = run.lease_report_type
        result = dict(summary[report_type])
        result.pop("error_reason", None)
        result.update({
            "status": "retryable_error",
            "error_code": STEP_ERROR_CODES[stage],
            "error_stage": stage,
            "correlation_id": correlation_id,
            "error": SAFE_ERROR_MESSAGE,
        })
        if error_reason in STAGE_ERROR_REASONS.get(stage, frozenset()):
            result["error_reason"] = error_reason
        summary[report_type] = result
        run.result_summary = summary
        run.error_message = SAFE_ERROR_MESSAGE
        progress = dict(run.progress)
        progress["step_state"] = "retryable_error"
        run.progress = progress
        _clear_lease(run)
        run.save(update_fields=[
            "result_summary", "error_message", "progress", "lease_token",
            "lease_report_type", "lease_chunk", "lease_started_at",
        ])
        return run


def _fail_revoked_permission(run, report_type):
    summary = dict(run.result_summary)
    result = dict(summary[report_type])
    result.update({
        "status": "failed",
        "error_code": "permission_revoked",
        "error": "Право на обновление данных было отозвано.",
    })
    summary[report_type] = result
    run.result_summary = summary
    run.error_message = "Право на обновление данных было отозвано."
    run.status = _terminal_status(summary)
    run.finished_at = timezone.now()
    progress = dict(run.progress)
    progress["step_state"] = "permission_revoked"
    run.progress = progress
    _clear_lease(run)
    run.save(update_fields=[
        "result_summary", "error_message", "status", "finished_at", "progress",
        "lease_token", "lease_report_type", "lease_chunk", "lease_started_at",
    ])
    return run


def step_unified_sync(
    run_id, user, allowed_report_types, expected_cursor, *, config=None, opener=None,
    mode=OneCODataSyncRun.MODE_PREVIEW,
):
    allowed = set(allowed_report_types)
    run, token = _claim_step(run_id, user, allowed, expected_cursor, mode)
    if token is None:
        return run
    item = dict(run.lease_chunk)
    report_type = run.lease_report_type
    try:
        config = validate_config(config or config_from_settings())
        client = opener or build_opener(NoRedirectHandler())
    except Exception as exc:
        return _safe_step_failure(run_id, token, expected_cursor, STAGE_CONFIG, exc)
    try:
        if report_type == REPORT_PROFIT:
            rows, pages = _collect_profit_chunk(item["start"], item["end"], config=config, opener=client)
            warnings = []
        else:
            rows, pages, warnings = _collect_cashflow_chunk(item["start"], item["end"], config=config, opener=client)
    except UnifiedSyncStageError as exc:
        return _safe_step_failure(
            run_id,
            token,
            expected_cursor,
            exc.stage,
            exc.__cause__ or exc,
            error_reason=exc.error_reason,
        )
    except Exception as exc:
        stage = STAGE_PROFIT_READ if report_type == REPORT_PROFIT else STAGE_CASHFLOW_READ
        return _safe_step_failure(run_id, token, expected_cursor, stage, exc)

    created_files = []
    try:
        with transaction.atomic():
            locked = OneCODataSyncRun.objects.select_for_update().select_related("organization").get(
                pk=run.pk, mode=mode
            )
            cursor = dict(locked.cursor)
            if str(locked.lease_token or "") != str(token) or int(cursor.get("version", 0)) != int(expected_cursor):
                return locked
            if (
                locked.lease_report_type != report_type
                or dict(locked.lease_chunk) != item
                or not _has_report_permission(user, locked.organization, report_type)
            ):
                if locked.lease_report_type == report_type and dict(locked.lease_chunk) == item:
                    return _fail_revoked_permission(locked, report_type)
                return locked
            summary = dict(locked.result_summary)
            report_result = dict(summary[report_type])
            report_result.update({"status": "running", "error": "", "error_code": ""})
            report_result.setdefault("reactivation_candidates", [])
            sync_scope = dict(locked.sync_scope)
            baseline = list(sync_scope.get("_baseline", []))
            apply_plan = list(sync_scope.get("_apply_plan", []))
            for month in _months(date.fromisoformat(item["start"]), date.fromisoformat(item["end"])):
                month_rows = [row for row in rows if date.fromisoformat(row["period_month"]) == month]
                new_fingerprint = month_fingerprint(report_type, month, month_rows)
                active_rows = _active_rows(locked.organization, report_type, month)
                active_fingerprint = month_fingerprint(report_type, month, active_rows)
                has_active = OneCReportPeriodState.objects.filter(
                    organization=locked.organization, report_type=report_type, period_month=month,
                    active_batch__status=OneCImportBatch.STATUS_CONFIRMED,
                ).exists()
                active_state = OneCReportPeriodState.objects.filter(
                    organization=locked.organization,
                    report_type=report_type,
                    period_month=month,
                ).select_related("active_batch").first()
                baseline_item = {
                    "report_type": report_type,
                    "month": month.isoformat(),
                    "expected_active_batch_id": str(active_state.active_batch_id) if active_state else None,
                    "expected_active_fingerprint": active_fingerprint,
                    "source_status": "authoritative_empty" if not month_rows else "complete",
                }
                if locked.mode == OneCODataSyncRun.MODE_AUTO_APPLY:
                    baseline = [entry for entry in baseline if not (
                        entry.get("report_type") == report_type and entry.get("month") == month.isoformat()
                    )]
                    baseline.append(baseline_item)
                if (
                    (
                        locked.mode == OneCODataSyncRun.MODE_PREVIEW
                        and not has_active
                        and not month_rows
                    )
                    or (has_active and new_fingerprint == active_fingerprint)
                ):
                    baseline_item["source_status"] = "unchanged"
                    if month.isoformat() not in report_result["unchanged_months"]:
                        report_result["unchanged_months"].append(month.isoformat())
                    continue
                if month_rows:
                    baseline_item["source_status"] = "changed"
                candidate = None if locked.mode == OneCODataSyncRun.MODE_AUTO_APPLY else _confirmed_candidate(
                    locked.organization, report_type, month, new_fingerprint
                )
                if candidate:
                    active_state = OneCReportPeriodState.objects.filter(
                        organization=locked.organization,
                        report_type=report_type,
                        period_month=month,
                    ).select_related("active_batch").first()
                    value = {
                        "month": month.isoformat(),
                        "report_type": report_type,
                        "candidate_batch_id": str(candidate.id),
                        "candidate_fingerprint": new_fingerprint,
                        "expected_active_batch_id": str(active_state.active_batch_id) if active_state else None,
                        "expected_active_fingerprint": active_fingerprint,
                    }
                    if value not in report_result["reactivation_candidates"]:
                        report_result["reactivation_candidates"].append(value)
                else:
                    batch, _, created = _create_month_draft(
                        locked, report_type, month, month_rows, pages, config, warnings
                    )
                    if created and batch.stored_file:
                        created_files.append((batch.pk, batch))
                    if str(batch.id) not in report_result["drafts"]:
                        report_result["drafts"].append(str(batch.id))
                    if locked.mode == OneCODataSyncRun.MODE_AUTO_APPLY:
                        value = {
                            "report_type": report_type,
                            "month": month.isoformat(),
                            "batch_id": str(batch.id),
                            "fingerprint": new_fingerprint,
                        }
                        apply_plan = [entry for entry in apply_plan if not (
                            entry.get("report_type") == report_type and entry.get("month") == month.isoformat()
                        )]
                        apply_plan.append(value)
                if month.isoformat() not in report_result["changed_months"]:
                    report_result["changed_months"].append(month.isoformat())
            queue = cursor.get("queue", [])
            index = int(cursor.get("index", 0))
            next_index = index + 1
            next_report = queue[next_index]["report_type"] if next_index < len(queue) else None
            if next_report != report_type:
                report_result["status"] = "completed"
            summary[report_type] = report_result
            cursor.update({"index": next_index, "version": int(expected_cursor) + 1})
            progress = dict(locked.progress)
            progress.update({"completed_chunks": min(next_index, len(queue)), "step_state": "idle"})
            locked.cursor = cursor
            locked.progress = progress
            locked.result_summary = summary
            if locked.mode == OneCODataSyncRun.MODE_AUTO_APPLY:
                sync_scope["_baseline"] = baseline
                sync_scope["_apply_plan"] = apply_plan
                locked.sync_scope = sync_scope
            locked.error_message = ""
            _clear_lease(locked)
            if next_index >= len(queue):
                locked.status = _terminal_status(summary)
                locked.finished_at = timezone.now()
            locked.save()
            completed = next_index >= len(queue)
            result = locked
    except Exception:
        for batch_id, batch in created_files:
            if not OneCImportBatch.objects.filter(pk=batch_id).exists():
                delete_private_batch_file(batch)
        if mode == OneCODataSyncRun.MODE_AUTO_APPLY:
            return _fail_auto_run(
                run_id, "candidate_invalid", "Не удалось проверить собранные данные 1С."
            )
        raise
    if completed and mode == OneCODataSyncRun.MODE_AUTO_APPLY:
        return apply_auto_sync(run_id, user, allowed, config=config)
    return result


def reactivate_confirmed_candidate(run_id, organization, user, report_type, month, batch_id, fingerprint):
    month = date.fromisoformat(month) if isinstance(month, str) else month
    with transaction.atomic():
        locked_organization = Organization.objects.select_for_update().get(pk=organization.pk)
        if not _has_report_permission(user, locked_organization, report_type):
            raise PermissionDenied("Permission for this 1C report type is required.")
        run = OneCODataSyncRun.objects.select_for_update().get(
            pk=run_id, organization=locked_organization, mode=OneCODataSyncRun.MODE_PREVIEW
        )
        candidates = run.result_summary.get(report_type, {}).get("reactivation_candidates", [])
        candidate = next((item for item in candidates if (
            item.get("month") == month.isoformat()
            and item.get("report_type") == report_type
            and item.get("candidate_batch_id") == str(batch_id)
            and item.get("candidate_fingerprint") == fingerprint
        )), None)
        if candidate is None:
            raise UnifiedSyncError("Версия не является кандидатом этой проверки.")
        batch = OneCImportBatch.objects.select_for_update().get(
            pk=batch_id, organization=locked_organization, import_type=report_type,
            source_type=OneCImportBatch.SOURCE_ODATA,
            status=OneCImportBatch.STATUS_CONFIRMED,
            sync_run__isnull=True,
        )
        validate_period_assignment(batch, locked_organization, report_type, month)
        model = OneCMonthlyProfit if report_type == REPORT_PROFIT else CashFlowRow
        if month_fingerprint(report_type, month, model.objects.filter(import_batch=batch, period_month=month)) != fingerprint:
            raise UnifiedSyncError("Данные подтверждённой версии изменились.")
        states = list(OneCReportPeriodState.objects.select_for_update().filter(
            organization=locked_organization, report_type=report_type, period_month=month,
        ).select_related("active_batch"))
        current_state = states[0] if states else None
        current_batch_id = current_state.active_batch_id if current_state else None
        current_rows = _active_rows(locked_organization, report_type, month) if current_state else []
        current_fingerprint = month_fingerprint(report_type, month, current_rows)
        if current_batch_id == batch.id and current_fingerprint == fingerprint:
            return batch
        expected_active_id = candidate.get("expected_active_batch_id")
        if (
            (str(current_batch_id) if current_batch_id else None) != expected_active_id
            or current_fingerprint != candidate.get("expected_active_fingerprint")
        ):
            raise StaleReactivationError(
                "Активная версия месяца изменилась после проверки. Запустите обновление из 1С повторно"
            )
        _activate_period_states(batch, locked_organization, user, [month], states)
        return batch
