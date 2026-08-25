from __future__ import annotations

import hashlib
import logging
import time
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from pool_service.models import (
    DataAuditLog,
    CashFlowRow,
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCReportPeriodActivation,
    OneCReportPeriodState,
    Organization,
    PayrollRow,
    onec_monthly_profit_source_identity,
)
from .monthly_profit_parser import PARSER_VERSION, MonthlyProfitParseError, parse_monthly_profit
from .monthly_profit_parser import classify_nomenclature_type
from .validators import delete_private_batch_file, delete_private_file, safe_original_filename, stream_sha256

logger = logging.getLogger(__name__)
PREVIEW_ROW_LIMIT = 30
METADATA_WARNING_LIMIT = 50
METADATA_WARNING_MAX_LENGTH = 300
ERROR_MESSAGE_MAX_LENGTH = 500
PROFITABILITY_QUANTUM = Decimal("0.0001")
MONEY_QUANTUM = Decimal("0.01")
RATIO_QUANTUM = Decimal("0.0000000001")


class DuplicateImportError(ValidationError):
    def __init__(self, batch):
        self.batch = batch
        super().__init__("Этот файл уже был загружен для текущей организации.")


def apply_period_weighted_goods_cost(records):
    """Add deterministic analytics without changing source 1C values."""
    bases = {}
    for row in records:
        if (
            classify_nomenclature_type(row.get("nomenclature_type")) == "goods"
            and row.get("cost") is not None and row["cost"] > 0
            and row.get("revenue") is not None and row["revenue"] > 0
        ):
            revenue, cost = bases.get(row["period_month"], (Decimal("0"), Decimal("0")))
            bases[row["period_month"]] = (revenue + row["revenue"], cost + row["cost"])

    ratios = {
        period: (cost / revenue).quantize(RATIO_QUANTUM, rounding=ROUND_HALF_UP)
        for period, (revenue, cost) in bases.items() if revenue > 0
    }
    for row in records:
        row["calculated_cost"] = None
        row["cost_calculation_method"] = ""
        row["cost_calculation_ratio"] = None
        row["analytical_gross_profit"] = row.get("gross_profit")
        is_zero_cost_goods = (
            classify_nomenclature_type(row.get("nomenclature_type")) == "goods"
            and row.get("cost") == 0
        )
        if not is_zero_cost_goods:
            row["cost_source"] = OneCMonthlyProfit.COST_SOURCE_ACTUAL
            continue
        ratio = ratios.get(row["period_month"])
        revenue = row.get("revenue")
        if ratio is None or revenue is None or revenue <= 0:
            row["cost_source"] = OneCMonthlyProfit.COST_SOURCE_UNDEFINED
            row["analytical_gross_profit"] = None
            continue
        calculated = (revenue * ratio).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
        row["cost_source"] = OneCMonthlyProfit.COST_SOURCE_CALCULATED
        row["calculated_cost"] = calculated
        row["cost_calculation_method"] = OneCMonthlyProfit.COST_METHOD_PERIOD_WEIGHTED_GOODS
        row["cost_calculation_ratio"] = ratio
        row["analytical_gross_profit"] = (revenue - calculated).quantize(
            MONEY_QUANTUM, rounding=ROUND_HALF_UP
        )
    return records


def _log(batch, user, event, **details):
    logger.info(
        "1C import event=%s batch=%s organization=%s user=%s status=%s rows=%s details=%s",
        event, batch.id, batch.organization_id, getattr(user, "id", None), batch.status,
        batch.rows_imported or batch.rows_detected, details,
    )


def _audit(batch, user, before, after, *, audit_context=None):
    audit_after = dict(after)
    if audit_context:
        audit_after["confirmation_context"] = dict(audit_context)
    DataAuditLog.objects.create(
        entity_type="OneCImportBatch", entity_id=str(batch.id), action=DataAuditLog.ACTION_UPDATE,
        organization=batch.organization, actor=user, before=before, after=audit_after,
        changed_fields=sorted(set(before) | set(audit_after)),
        ip_address=(audit_context or {}).get("remote_ip"),
        user_agent=(audit_context or {}).get("user_agent", "")[:512],
    )


def _preview_metadata(result, overlap_months=()):
    totals = {key: Decimal("0") for key in ("revenue", "cost", "gross_profit")}
    for row in result.records:
        totals["revenue"] += row.get("revenue") or Decimal("0")
        effective_cost = (
            row.get("calculated_cost")
            if row.get("cost_source") == OneCMonthlyProfit.COST_SOURCE_CALCULATED
            else row.get("cost")
        )
        effective_profit = (
            row.get("analytical_gross_profit")
            if row.get("cost_source")
            else row.get("gross_profit")
        )
        totals["cost"] += effective_cost or Decimal("0")
        totals["gross_profit"] += effective_profit or Decimal("0")
    sample = []
    for row in result.records[:PREVIEW_ROW_LIMIT]:
        sample.append({
            key: value.isoformat() if hasattr(value, "isoformat") else str(value) if isinstance(value, Decimal) else value
            for key, value in row.items() if key != "source_data"
        })
    warnings = [str(item)[:METADATA_WARNING_MAX_LENGTH] for item in result.warnings[:METADATA_WARNING_LIMIT]]
    warnings_total = max(result.warnings_total, len(result.warnings))
    totals["profitability_percent"] = calculate_profitability(totals["gross_profit"], totals["revenue"])
    overlap_months = sorted(set(overlap_months))
    return {
        "report": result.metadata, "preview": sample,
        "totals": {key: str(value) if value is not None else None for key, value in totals.items()},
        "warnings": warnings,
        "warnings_total": warnings_total,
        "warnings_hidden": max(warnings_total - len(warnings), 0),
        "critical_errors": [str(item)[:METADATA_WARNING_MAX_LENGTH] for item in result.critical_errors[:20]],
        "overlap_months": [month.strftime("%Y-%m") for month in overlap_months],
        "overlap_count": len(overlap_months),
    }


def calculate_profitability(gross_profit, revenue):
    revenue = Decimal(revenue or 0)
    if revenue == 0:
        return None
    return (Decimal(gross_profit or 0) * Decimal("100") / revenue).quantize(
        PROFITABILITY_QUANTUM, rounding=ROUND_HALF_UP
    )


def _result_periods(result):
    periods = sorted({row["period_month"] for row in result.records})
    if any(period.day != 1 for period in periods):
        raise ValidationError("Месяц отчёта должен начинаться с первого числа.")
    return periods


def overlap_months_for(organization, report_type, periods):
    if not periods:
        return []
    return list(
        OneCReportPeriodState.objects.filter(
            organization=organization,
            report_type=report_type,
            period_month__in=periods,
        )
        .order_by("period_month")
        .values_list("period_month", flat=True)
    )


def validate_period_assignment(batch, organization, report_type, period_month):
    if batch.organization_id != organization.pk:
        raise ValidationError("Активная загрузка принадлежит другой организации.")
    if batch.import_type != report_type:
        raise ValidationError("Тип активной загрузки не совпадает с типом отчёта.")
    if batch.status != OneCImportBatch.STATUS_CONFIRMED:
        raise ValidationError("Активной может быть только подтверждённая загрузка.")
    if period_month.day != 1:
        raise ValidationError("Месяц активной версии должен начинаться с первого числа.")
    row_model = {
        OneCImportBatch.TYPE_MONTHLY_PROFIT: OneCMonthlyProfit,
        OneCImportBatch.TYPE_PAYROLL: PayrollRow,
        OneCImportBatch.TYPE_CASHFLOW: CashFlowRow,
    }.get(report_type)
    if row_model is None:
        raise ValidationError("Неизвестный тип отчёта 1С.")
    has_rows = row_model.objects.filter(
        import_batch=batch,
        organization=organization,
        period_month=period_month,
    ).exists()
    if has_rows:
        return
    scope_months = batch.metadata.get("scope_months", [])
    expected_scope = []
    current = batch.period_first
    while current is not None and batch.period_last is not None and current <= batch.period_last:
        expected_scope.append(current.isoformat())
        current = date(
            current.year + (current.month == 12),
            1 if current.month == 12 else current.month + 1,
            1,
        )
    is_explicit_empty_odata_month = (
        report_type in (
            OneCImportBatch.TYPE_MONTHLY_PROFIT,
            OneCImportBatch.TYPE_CASHFLOW,
        )
        and batch.source_type == OneCImportBatch.SOURCE_ODATA
        and scope_months == expected_scope
        and period_month.isoformat() in scope_months
        and batch.period_first is not None
        and batch.period_last is not None
        and batch.period_first <= period_month <= batch.period_last
    )
    if not is_explicit_empty_odata_month:
        raise ValidationError("В активной загрузке отсутствуют строки указанного месяца.")


def _bulk_create_monthly_rows(rows):
    for row in rows:
        row.source_identity = onec_monthly_profit_source_identity(
            period_month=row.period_month,
            source_row_number=row.source_row_number,
            source_recorder=row.source_recorder,
        )
    OneCMonthlyProfit.objects.bulk_create(rows, batch_size=500)


def _activate_period_states(batch, organization, user, periods, locked_states):
    states_by_month = {state.period_month: state for state in locked_states}
    for period_month in periods:
        state = states_by_month.get(period_month)
        replaced_batch = state.active_batch if state else None
        if replaced_batch is not None:
            validate_period_assignment(
                replaced_batch, organization, batch.import_type, period_month
            )
        validate_period_assignment(batch, organization, batch.import_type, period_month)
        if state is None:
            state = OneCReportPeriodState.objects.create(
                organization=organization,
                report_type=batch.import_type,
                period_month=period_month,
                active_batch=batch,
                updated_by=user,
            )
        else:
            state.active_batch = batch
            state.updated_by = user
            state.save(update_fields=["active_batch", "updated_by", "updated_at"])
        OneCReportPeriodActivation.objects.create(
            period_state=state,
            batch=batch,
            replaced_batch=replaced_batch,
            activated_by=user,
        )


def _save_confirmed_batch(batch, user, rows_count):
    batch.confirmed_by = user
    batch.confirmed_at = timezone.now()
    batch.rows_imported = rows_count
    batch.error_message = ""
    batch.save(
        update_fields=[
            "status",
            "confirmed_by",
            "confirmed_at",
            "rows_imported",
            "error_message",
        ]
    )


def _repreview_obsolete_batch(batch_id, uploaded_file, organization, user, digest):
    with transaction.atomic():
        batch = OneCImportBatch.objects.select_for_update().get(
            id=batch_id, organization=organization
        )
        if (
            batch.status == OneCImportBatch.STATUS_CONFIRMED
            or batch.parser_version == PARSER_VERSION
        ):
            raise DuplicateImportError(batch)

        uploaded_file.seek(0)
        result = parse_monthly_profit(
            uploaded_file,
            filename=safe_original_filename(uploaded_file.name),
            size=uploaded_file.size,
        )
        apply_period_weighted_goods_cost(result.records)
        periods = _result_periods(result)
        overlap_months = overlap_months_for(
            organization, batch.import_type, periods
        )

        stored_is_current = False
        try:
            stored_is_current = bool(batch.stored_file) and _stored_sha256(batch) == digest
        except (OSError, ValueError):
            stored_is_current = False
        if not stored_is_current:
            uploaded_file.seek(0)
            generated_name = batch.stored_file.field.generate_filename(batch, "source.xlsx")
            batch.stored_file.name = batch.stored_file.storage.save(
                generated_name, uploaded_file
            )

        before = {"status": batch.status, "parser_version": batch.parser_version}
        batch.original_filename = safe_original_filename(uploaded_file.name)
        batch.file_size = uploaded_file.size
        batch.uploaded_by = user
        batch.parser_version = PARSER_VERSION
        batch.status = OneCImportBatch.STATUS_PREVIEWED
        batch.rows_detected = len(result.records)
        batch.rows_imported = 0
        batch.warnings_count = max(result.warnings_total, len(result.warnings))
        batch.metadata = _preview_metadata(result, overlap_months)
        batch.error_message = ""
        batch.save(update_fields=[
            "stored_file", "original_filename", "file_size", "uploaded_by",
            "parser_version", "status", "rows_detected", "rows_imported",
            "warnings_count", "metadata", "error_message",
        ])
        _audit(batch, user, before, {
            "status": batch.status, "parser_version": batch.parser_version,
        })
    _log(batch, user, "repreviewed", obsolete_parser=before["parser_version"])
    return batch


def create_monthly_profit_preview(uploaded_file, organization, user):
    started = time.monotonic()
    digest = getattr(uploaded_file, "file_sha256", None) or stream_sha256(uploaded_file)
    duplicate = OneCImportBatch.objects.filter(
        organization=organization, import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
        file_sha256=digest,
    ).first()
    if duplicate:
        if (
            duplicate.status != OneCImportBatch.STATUS_CONFIRMED
            and duplicate.parser_version != PARSER_VERSION
        ):
            return _repreview_obsolete_batch(
                duplicate.id, uploaded_file, organization, user, digest
            )
        raise DuplicateImportError(duplicate)
    batch = OneCImportBatch(
        organization=organization, original_filename=safe_original_filename(uploaded_file.name),
        file_sha256=digest, file_size=uploaded_file.size, uploaded_by=user, parser_version=PARSER_VERSION,
    )
    generated_name = batch.stored_file.field.generate_filename(batch, "source.xlsx")
    try:
        stored_name = batch.stored_file.storage.save(generated_name, uploaded_file)
        batch.stored_file.name = stored_name
    except Exception:
        batch.stored_file.name = generated_name
        delete_private_batch_file(batch)
        raise
    try:
        with transaction.atomic():
            batch.save()
    except IntegrityError:
        delete_private_batch_file(batch)
        duplicate = OneCImportBatch.objects.get(
            organization=organization, import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            file_sha256=digest,
        )
        raise DuplicateImportError(duplicate)
    except Exception as exc:
        delete_private_batch_file(batch)
        logger.exception(
            "1C import batch save failed batch=%s organization=%s user=%s error_type=%s",
            batch.id, organization.id, getattr(user, "id", None), type(exc).__name__,
        )
        raise
    try:
        with batch.stored_file.open("rb") as source:
            result = parse_monthly_profit(source, filename=batch.original_filename, size=batch.file_size)
        apply_period_weighted_goods_cost(result.records)
        periods = _result_periods(result)
        overlap_months = overlap_months_for(
            organization, batch.import_type, periods
        )
        batch.status = OneCImportBatch.STATUS_PREVIEWED
        batch.rows_detected = len(result.records)
        batch.warnings_count = max(result.warnings_total, len(result.warnings))
        batch.metadata = _preview_metadata(result, overlap_months)
        batch.error_message = ""
        batch.save(update_fields=["status", "rows_detected", "warnings_count", "metadata", "error_message"])
        _audit(batch, user, {"status": "uploaded"}, {"status": "previewed"})
        _log(batch, user, "previewed", duration_ms=int((time.monotonic() - started) * 1000))
        return batch
    except (MonthlyProfitParseError, ValidationError, OSError) as exc:
        safe_message = str(exc) if isinstance(exc, (MonthlyProfitParseError, ValidationError)) else "Ошибка приватного хранилища."
        batch.status, batch.error_message = OneCImportBatch.STATUS_FAILED, safe_message[:ERROR_MESSAGE_MAX_LENGTH]
        batch.save(update_fields=["status", "error_message"])
        _audit(batch, user, {"status": "uploaded"}, {"status": "failed"})
        _log(batch, user, "failed", error_type=type(exc).__name__)
        raise


def _stored_sha256(batch):
    digest = hashlib.sha256()
    with batch.stored_file.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()


def confirm_monthly_profit(batch_id, organization, user):
    try:
        with transaction.atomic():
            locked_organization = Organization.objects.select_for_update().get(
                pk=organization.pk
            )
            batch = OneCImportBatch.objects.select_for_update().get(
                id=batch_id, organization=locked_organization
            )
            if batch.status != OneCImportBatch.STATUS_PREVIEWED:
                raise ValidationError("Подтвердить можно только импорт в статусе previewed.")
            if batch.parser_version != PARSER_VERSION:
                raise ValidationError(
                    "Предпросмотр создан устаревшей версией парсера. "
                    "Загрузите этот же файл повторно и проверьте новый предпросмотр."
                )
            if _stored_sha256(batch) != batch.file_sha256:
                raise ValidationError("Контрольная сумма исходного файла изменилась.")
            with batch.stored_file.open("rb") as source:
                result = parse_monthly_profit(source, filename=batch.original_filename, size=batch.file_size)
            apply_period_weighted_goods_cost(result.records)
            if result.critical_errors: raise ValidationError("; ".join(result.critical_errors))
            periods = _result_periods(result)
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
                for record in result.records
            ]
            _bulk_create_monthly_rows(rows)
            before = {"status": batch.status, "rows_imported": batch.rows_imported}
            batch.status = OneCImportBatch.STATUS_CONFIRMED
            _activate_period_states(
                batch,
                locked_organization,
                user,
                periods,
                locked_states,
            )
            _save_confirmed_batch(batch, user, len(rows))
            _audit(batch, user, before, {"status": batch.status, "rows_imported": batch.rows_imported})
        _log(batch, user, "confirmed", active_months=len(periods))
        return batch
    except OneCImportBatch.DoesNotExist:
        raise
    except Exception as exc:
        safe_message = f"Не удалось подтвердить импорт. Тип ошибки: {type(exc).__name__}."[:ERROR_MESSAGE_MAX_LENGTH]
        updated = OneCImportBatch.objects.filter(
            id=batch_id, organization=organization, status=OneCImportBatch.STATUS_PREVIEWED,
        ).update(status=OneCImportBatch.STATUS_FAILED, error_message=safe_message)
        if updated:
            failed_batch = OneCImportBatch.objects.get(id=batch_id, organization=organization)
            _audit(failed_batch, user, {"status": "previewed"}, {"status": "failed"})
        log_method = logger.warning if isinstance(exc, ValidationError) else logger.exception
        log_method(
            "1C import confirmation failed batch=%s organization=%s user=%s error_type=%s",
            batch_id, organization.id, getattr(user, "id", None), type(exc).__name__,
        )
        raise


def cancel_onec_import_batch(batch, user):
    with transaction.atomic():
        locked = OneCImportBatch.objects.select_for_update().get(
            id=batch.id, organization_id=batch.organization_id
        )
        if locked.status == OneCImportBatch.STATUS_CONFIRMED:
            raise ValidationError("Подтверждённый импорт отменить нельзя.")
        if locked.status == OneCImportBatch.STATUS_CANCELLED:
            return locked
        before = {"status": locked.status}
        locked.status = OneCImportBatch.STATUS_CANCELLED
        locked.save(update_fields=["status"])
        _audit(locked, user, before, {"status": locked.status})
        if locked.stored_file:
            # Do not lose the diagnostic file if an outer database transaction
            # is rolled back after cancellation.
            storage = locked.stored_file.storage
            name = locked.stored_file.name
            organization_id = locked.organization_id
            batch_id = locked.id
            transaction.on_commit(
                lambda: delete_private_file(
                    storage, name, organization_id=organization_id, batch_id=batch_id
                )
            )
    _log(locked, user, "cancelled")
    return locked


def cancel_monthly_profit(batch, user):
    return cancel_onec_import_batch(batch, user)
