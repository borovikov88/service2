from django.db import migrations, models


def backfill_cashflow_source_identity(apps, schema_editor):
    CashFlowRow = apps.get_model("pool_service", "CashFlowRow")
    pending = []
    for row in CashFlowRow.objects.only(
        "pk", "period_month", "source_row_number"
    ).iterator(chunk_size=500):
        row.source_identity = (
            f"xlsx:{row.period_month.isoformat()}:{int(row.source_row_number)}"
        )
        pending.append(row)
        if len(pending) >= 500:
            CashFlowRow.objects.bulk_update(
                pending, ["source_identity"], batch_size=500
            )
            pending = []
    if pending:
        CashFlowRow.objects.bulk_update(
            pending, ["source_identity"], batch_size=500
        )


def clear_cashflow_source_identity(apps, schema_editor):
    apps.get_model("pool_service", "CashFlowRow").objects.update(
        source_identity=None
    )


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0095_onec_odata_draft_fields")]

    operations = [
        migrations.AddField(
            model_name="cashflowrow",
            name="source_identity",
            field=models.CharField(max_length=80, null=True),
        ),
        migrations.RunPython(
            backfill_cashflow_source_identity,
            clear_cashflow_source_identity,
        ),
        migrations.AlterField(
            model_name="cashflowrow",
            name="source_identity",
            field=models.CharField(max_length=80),
        ),
        migrations.RemoveConstraint(
            model_name="cashflowrow",
            name="unique_cashflow_batch_row_month",
        ),
        migrations.AddConstraint(
            model_name="cashflowrow",
            constraint=models.UniqueConstraint(
                fields=("import_batch", "source_identity"),
                name="unique_cashflow_batch_source_identity",
            ),
        ),
    ]
