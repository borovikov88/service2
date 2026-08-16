from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from pool_service.models import (
    CashFlowRow,
    OneCImportBatch,
    OneCReportPeriodState,
    Organization,
    PayrollRow,
)
from .employee_matching import resolve_employee_identity
from .services import (
    DuplicateImportError,
    _activate_period_states,
    _audit,
    _stored_sha256,
    overlap_months_for,
)
from .validators import delete_private_batch_file, safe_original_filename, stream_sha256


PREVIEW_ROW_LIMIT = 30


def _serialize(value):
    if isinstance(value, (Decimal, date)):
        return str(value)
    return value


def _metadata(result, overlap_months):
    preview = [
        {key: _serialize(value) for key, value in row.items() if key != "source_data"}
        for row in result.records[:PREVIEW_ROW_LIMIT]
    ]
    metadata = {
        "report": result.metadata,
        "preview": preview,
        "warnings": [str(item)[:300] for item in result.warnings[:50]],
        "critical_errors": [str(item)[:300] for item in result.critical_errors[:20]],
        "overlap_months": [month.strftime("%Y-%m") for month in overlap_months],
    }
    if result.records and "employee_raw_name" in result.records[0]:
        metadata["payroll_summary"] = {
            "distinct_employees": len({
                (row["employee_normalized_name"], row["department_name"])
                for row in result.records
            }),
            "departments": sorted({row["department_name"] for row in result.records if row["department_name"]}),
            "opening_balance": str(sum(
                (row["opening_balance"] for row in result.records), Decimal("0")
            )),
            "accrued": str(sum((row["accrued"] for row in result.records), Decimal("0"))),
            "paid": str(sum((row["paid"] for row in result.records), Decimal("0"))),
            "closing_balance": str(sum(
                (row["closing_balance"] for row in result.records), Decimal("0")
            )),
        }
    return metadata


def _parse_upload(uploaded_file, parser):
    uploaded_file.seek(0)
    return parser(
        uploaded_file,
        filename=safe_original_filename(uploaded_file.name),
        size=uploaded_file.size,
    )


def create_foundation_preview(
    uploaded_file, organization, user, *, import_type, parser, parser_version
):
    digest = getattr(uploaded_file, "file_sha256", None) or stream_sha256(uploaded_file)
    duplicate = OneCImportBatch.objects.filter(
        organization=organization, import_type=import_type, file_sha256=digest
    ).first()
    if duplicate and (
        duplicate.status == OneCImportBatch.STATUS_CONFIRMED
        or duplicate.parser_version == parser_version
    ):
        raise DuplicateImportError(duplicate)
    result = _parse_upload(uploaded_file, parser)
    periods = sorted({row["period_month"] for row in result.records})
    overlap = overlap_months_for(organization, import_type, periods)
    if duplicate:
        with transaction.atomic():
            batch = OneCImportBatch.objects.select_for_update().get(
                pk=duplicate.pk, organization=organization
            )
            if _stored_sha256(batch) != digest:
                raise ValidationError("Сохранённый исходный файл не совпадает с повторной загрузкой.")
            before = {"status": batch.status, "parser_version": batch.parser_version}
            batch.status = OneCImportBatch.STATUS_PREVIEWED
            batch.parser_version = parser_version
            batch.original_filename = safe_original_filename(uploaded_file.name)
            batch.rows_detected = len(result.records)
            batch.rows_imported = 0
            batch.period_first = periods[0]
            batch.period_last = periods[-1]
            batch.warnings_count = len(result.warnings)
            batch.error_message = ""
            batch.metadata = _metadata(result, overlap)
            batch.save()
            _audit(batch, user, before, {
                "status": batch.status, "parser_version": batch.parser_version,
            })
            return batch

    batch = OneCImportBatch(
        organization=organization,
        import_type=import_type,
        original_filename=safe_original_filename(uploaded_file.name),
        file_sha256=digest,
        file_size=uploaded_file.size,
        uploaded_by=user,
        parser_version=parser_version,
        period_first=periods[0],
        period_last=periods[-1],
    )
    generated_name = batch.stored_file.field.generate_filename(batch, "source.xlsx")
    uploaded_file.seek(0)
    batch.stored_file.name = batch.stored_file.storage.save(generated_name, uploaded_file)
    try:
        with transaction.atomic():
            batch.status = OneCImportBatch.STATUS_PREVIEWED
            batch.rows_detected = len(result.records)
            batch.warnings_count = len(result.warnings)
            batch.metadata = _metadata(result, overlap)
            batch.save()
            _audit(batch, user, {"status": "uploaded"}, {"status": "previewed"})
    except IntegrityError:
        delete_private_batch_file(batch)
        duplicate = OneCImportBatch.objects.get(
            organization=organization, import_type=import_type, file_sha256=digest
        )
        raise DuplicateImportError(duplicate)
    except Exception:
        delete_private_batch_file(batch)
        raise
    return batch


def _build_rows(batch, organization, result):
    if batch.import_type == OneCImportBatch.TYPE_PAYROLL:
        rows = []
        for parsed in result.records:
            record = dict(parsed)
            personnel_number = record.pop("personnel_number", "")
            identity = resolve_employee_identity(
                organization,
                record["employee_raw_name"],
                department_name=record["department_name"],
                personnel_number=personnel_number,
            )
            rows.append(PayrollRow(
                import_batch=batch, organization=organization,
                employee_identity=identity, **record,
            ))
        return PayrollRow, rows
    if batch.import_type == OneCImportBatch.TYPE_CASHFLOW:
        return CashFlowRow, [
            CashFlowRow(import_batch=batch, organization=organization, **record)
            for record in result.records
        ]
    raise ValidationError("Неподдерживаемый тип foundation import.")


def confirm_foundation_import(
    batch_id, organization, user, *, import_type, parser, parser_version
):
    with transaction.atomic():
        locked_organization = Organization.objects.select_for_update().get(pk=organization.pk)
        batch = OneCImportBatch.objects.select_for_update().get(
            pk=batch_id, organization=locked_organization, import_type=import_type
        )
        if batch.status != OneCImportBatch.STATUS_PREVIEWED:
            raise ValidationError("Подтвердить можно только импорт в статусе previewed.")
        if batch.parser_version != parser_version:
            raise ValidationError(
                "Предпросмотр создан устаревшей версией парсера. Повторите предпросмотр."
            )
        if _stored_sha256(batch) != batch.file_sha256:
            raise ValidationError("Контрольная сумма исходного файла изменилась.")
        with batch.stored_file.open("rb") as source:
            result = parser(source, filename=batch.original_filename, size=batch.file_size)
        if result.critical_errors:
            raise ValidationError("; ".join(result.critical_errors))
        periods = sorted({row["period_month"] for row in result.records})
        locked_states = list(
            OneCReportPeriodState.objects.select_for_update()
            .filter(
                organization=locked_organization,
                report_type=import_type,
                period_month__in=periods,
            )
            .select_related("active_batch")
            .order_by("period_month")
        )
        row_model, rows = _build_rows(batch, locked_organization, result)
        row_model.objects.bulk_create(rows, batch_size=500)
        before = {"status": batch.status, "rows_imported": batch.rows_imported}
        batch.status = OneCImportBatch.STATUS_CONFIRMED
        batch.rows_imported = len(rows)
        batch.confirmed_by = user
        from django.utils import timezone
        batch.confirmed_at = timezone.now()
        batch.period_first = periods[0]
        batch.period_last = periods[-1]
        batch.error_message = ""
        batch.save()
        _activate_period_states(
            batch, locked_organization, user, periods, locked_states
        )
        _audit(batch, user, before, {
            "status": batch.status, "rows_imported": batch.rows_imported,
        })
    return batch
