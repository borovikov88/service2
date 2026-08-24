from django.db import migrations, models


def backfill_monthly_profit_source_identity(apps, schema_editor):
    MonthlyProfit = apps.get_model("pool_service", "OneCMonthlyProfit")
    pending = []
    for row in MonthlyProfit.objects.only(
        "pk", "period_month", "source_row_number"
    ).iterator(chunk_size=500):
        row.source_identity = (
            f"xlsx:{row.period_month.isoformat()}:{int(row.source_row_number)}"
        )
        pending.append(row)
        if len(pending) >= 500:
            MonthlyProfit.objects.bulk_update(
                pending, ["source_identity"], batch_size=500
            )
            pending = []
    if pending:
        MonthlyProfit.objects.bulk_update(
            pending, ["source_identity"], batch_size=500
        )


def clear_monthly_profit_source_identity(apps, schema_editor):
    MonthlyProfit = apps.get_model("pool_service", "OneCMonthlyProfit")
    MonthlyProfit.objects.update(source_identity=None)


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0094_mysql_identity_unique_constraints"),
    ]

    operations = [
        migrations.AddField(
            model_name="onecimportbatch",
            name="source_type",
            field=models.CharField(
                choices=[("xlsx", "XLSX"), ("odata", "OData")],
                default="xlsx",
                max_length=10,
                verbose_name="Источник",
            ),
        ),
        migrations.AddField(
            model_name="onecmonthlyprofit",
            name="source_recorder",
            field=models.UUIDField(blank=True, null=True, verbose_name="Регистратор 1С"),
        ),
        migrations.AddField(
            model_name="onecmonthlyprofit",
            name="source_identity",
            field=models.CharField(
                max_length=80,
                null=True,
                verbose_name="Идентификатор строки источника",
            ),
        ),
        migrations.RunPython(
            backfill_monthly_profit_source_identity,
            clear_monthly_profit_source_identity,
        ),
        migrations.AlterField(
            model_name="onecmonthlyprofit",
            name="source_identity",
            field=models.CharField(
                max_length=80,
                verbose_name="Идентификатор строки источника",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="onecmonthlyprofit",
            name="unique_onec_batch_row_month",
        ),
        migrations.AddConstraint(
            model_name="onecmonthlyprofit",
            constraint=models.UniqueConstraint(
                fields=("import_batch", "source_identity"),
                name="unique_onec_batch_source_identity",
            ),
        ),
    ]
