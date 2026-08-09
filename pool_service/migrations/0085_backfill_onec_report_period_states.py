from django.db import migrations, models, transaction
from django.db.models import F


def backfill_onec_report_period_states(apps, schema_editor):
    database = schema_editor.connection.alias
    ImportBatch = apps.get_model("pool_service", "OneCImportBatch")
    MonthlyProfit = apps.get_model("pool_service", "OneCMonthlyProfit")
    PeriodState = apps.get_model("pool_service", "OneCReportPeriodState")
    PeriodActivation = apps.get_model("pool_service", "OneCReportPeriodActivation")

    confirmed_rows = MonthlyProfit.objects.using(database).filter(
        import_batch__status="confirmed"
    )
    if confirmed_rows.exclude(
        organization_id=F("import_batch__organization_id")
    ).exists():
        raise RuntimeError(
            "Backfill active versions aborted: a confirmed 1C row belongs to a "
            "different organization than its batch."
        )

    scopes = list(
        confirmed_rows.values(
            "organization_id", "import_batch__import_type", "period_month"
        )
        .annotate(batch_count=models.Count("import_batch_id", distinct=True))
        .order_by("organization_id", "import_batch__import_type", "period_month")
    )
    conflicts = [scope for scope in scopes if scope["batch_count"] > 1]
    if conflicts:
        first = conflicts[0]
        raise RuntimeError(
            "Backfill active versions aborted: multiple confirmed 1C batches "
            "exist for organization={organization}, report_type={report_type}, "
            "period_month={period}. Resolve the active batch explicitly first."
            .format(
                organization=first["organization_id"],
                report_type=first["import_batch__import_type"],
                period=first["period_month"],
            )
        )
    invalid_month = next(
        (scope for scope in scopes if scope["period_month"].day != 1), None
    )
    if invalid_month:
        raise RuntimeError(
            "Backfill active versions aborted: period_month must be the first "
            f"day of a month ({invalid_month['period_month']})."
        )

    with transaction.atomic(using=database):
        for scope in scopes:
            batch_id = (
                confirmed_rows.filter(
                    organization_id=scope["organization_id"],
                    import_batch__import_type=scope["import_batch__import_type"],
                    period_month=scope["period_month"],
                )
                .values_list("import_batch_id", flat=True)
                .first()
            )
            batch = ImportBatch.objects.using(database).get(pk=batch_id)
            state, created = PeriodState.objects.using(database).get_or_create(
                organization_id=scope["organization_id"],
                report_type=scope["import_batch__import_type"],
                period_month=scope["period_month"],
                defaults={
                    "active_batch_id": batch_id,
                    "updated_by_id": batch.confirmed_by_id,
                },
            )
            if not created and state.active_batch_id != batch_id:
                raise RuntimeError(
                    "Backfill active versions aborted: an existing period state "
                    "points to a different batch."
                )
            PeriodActivation.objects.using(database).get_or_create(
                period_state_id=state.pk,
                batch_id=batch_id,
                defaults={
                    "replaced_batch_id": None,
                    "activated_by_id": batch.confirmed_by_id,
                },
            )


class Migration(migrations.Migration):

    dependencies = [
        (
            "pool_service",
            "0084_onecreportperiodstate_onecreportperiodactivation_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(
            backfill_onec_report_period_states,
            reverse_code=migrations.RunPython.noop,
            atomic=True,
        ),
    ]
