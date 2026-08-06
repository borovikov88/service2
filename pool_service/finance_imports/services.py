from __future__ import annotations

import hashlib
import logging
import time
from decimal import Decimal, ROUND_HALF_UP

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from pool_service.models import DataAuditLog, OneCImportBatch, OneCMonthlyProfit
from .monthly_profit_parser import PARSER_VERSION, MonthlyProfitParseError, parse_monthly_profit
from .validators import delete_private_batch_file, delete_private_file, safe_original_filename, stream_sha256

logger = logging.getLogger(__name__)
PREVIEW_ROW_LIMIT = 30
METADATA_WARNING_LIMIT = 50
METADATA_WARNING_MAX_LENGTH = 300
ERROR_MESSAGE_MAX_LENGTH = 500
PROFITABILITY_QUANTUM = Decimal("0.0001")


class DuplicateImportError(ValidationError):
    def __init__(self, batch):
        self.batch = batch
        super().__init__("Этот файл уже был загружен для текущей организации.")


def _log(batch, user, event, **details):
    logger.info(
        "1C import event=%s batch=%s organization=%s user=%s status=%s rows=%s details=%s",
        event, batch.id, batch.organization_id, getattr(user, "id", None), batch.status,
        batch.rows_imported or batch.rows_detected, details,
    )


def _audit(batch, user, before, after):
    DataAuditLog.objects.create(
        entity_type="OneCImportBatch", entity_id=str(batch.id), action=DataAuditLog.ACTION_UPDATE,
        organization=batch.organization, actor=user, before=before, after=after,
        changed_fields=sorted(set(before) | set(after)),
    )


def _preview_metadata(result):
    totals = {key: Decimal("0") for key in ("revenue", "cost", "gross_profit")}
    for row in result.records:
        for key in totals: totals[key] += row.get(key) or Decimal("0")
    sample = []
    for row in result.records[:PREVIEW_ROW_LIMIT]:
        sample.append({
            key: value.isoformat() if hasattr(value, "isoformat") else str(value) if isinstance(value, Decimal) else value
            for key, value in row.items() if key != "source_data"
        })
    warnings = [str(item)[:METADATA_WARNING_MAX_LENGTH] for item in result.warnings[:METADATA_WARNING_LIMIT]]
    warnings_total = max(result.warnings_total, len(result.warnings))
    totals["profitability_percent"] = calculate_profitability(totals["gross_profit"], totals["revenue"])
    return {
        "report": result.metadata, "preview": sample,
        "totals": {key: str(value) if value is not None else None for key, value in totals.items()},
        "warnings": warnings,
        "warnings_total": warnings_total,
        "warnings_hidden": max(warnings_total - len(warnings), 0),
        "critical_errors": [str(item)[:METADATA_WARNING_MAX_LENGTH] for item in result.critical_errors[:20]],
    }


def calculate_profitability(gross_profit, revenue):
    revenue = Decimal(revenue or 0)
    if revenue == 0:
        return None
    return (Decimal(gross_profit or 0) * Decimal("100") / revenue).quantize(
        PROFITABILITY_QUANTUM, rounding=ROUND_HALF_UP
    )


def create_monthly_profit_preview(uploaded_file, organization, user):
    started = time.monotonic()
    digest = getattr(uploaded_file, "file_sha256", None) or stream_sha256(uploaded_file)
    duplicate = OneCImportBatch.objects.filter(
        organization=organization, import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
        file_sha256=digest,
    ).first()
    if duplicate: raise DuplicateImportError(duplicate)
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
        batch.status = OneCImportBatch.STATUS_PREVIEWED
        batch.rows_detected = len(result.records)
        batch.warnings_count = max(result.warnings_total, len(result.warnings))
        batch.metadata = _preview_metadata(result)
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
            batch = OneCImportBatch.objects.select_for_update().get(id=batch_id, organization=organization)
            if batch.status != OneCImportBatch.STATUS_PREVIEWED:
                raise ValidationError("Подтвердить можно только импорт в статусе previewed.")
            if batch.metadata.get("critical_errors"):
                raise ValidationError("Подтверждение заблокировано критическими ошибками preview.")
            if _stored_sha256(batch) != batch.file_sha256:
                raise ValidationError("Контрольная сумма исходного файла изменилась.")
            with batch.stored_file.open("rb") as source:
                result = parse_monthly_profit(source, filename=batch.original_filename, size=batch.file_size)
            if result.critical_errors: raise ValidationError("; ".join(result.critical_errors))
            rows = [OneCMonthlyProfit(import_batch=batch, organization=organization, **record) for record in result.records]
            OneCMonthlyProfit.objects.bulk_create(rows, batch_size=500)
            before = {"status": batch.status, "rows_imported": batch.rows_imported}
            batch.status, batch.confirmed_by, batch.confirmed_at = OneCImportBatch.STATUS_CONFIRMED, user, timezone.now()
            batch.rows_imported, batch.error_message = len(rows), ""
            batch.save(update_fields=["status", "confirmed_by", "confirmed_at", "rows_imported", "error_message"])
            _audit(batch, user, before, {"status": batch.status, "rows_imported": batch.rows_imported})
        _log(batch, user, "confirmed")
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


def cancel_monthly_profit(batch, user):
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
