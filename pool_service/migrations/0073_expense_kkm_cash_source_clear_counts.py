from django.db import migrations, models


def clear_cash_counts(apps, schema_editor):
    CashCount = apps.get_model("pool_service", "CashCount")
    CashCount.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ("pool_service", "0072_cash_count_adjustments"),
    ]

    operations = [
        migrations.AlterField(
            model_name="expense",
            name="source",
            field=models.CharField(
                choices=[
                    ("accountable", "Подотчёт"),
                    ("company_cash", "Касса компании"),
                    ("kkm_cash", "Касса ККМ"),
                ],
                max_length=24,
            ),
        ),
        migrations.RunPython(clear_cash_counts, migrations.RunPython.noop),
    ]
