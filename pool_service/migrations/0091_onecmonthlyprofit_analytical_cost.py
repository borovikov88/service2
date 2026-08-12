from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("pool_service", "0090_alter_developmenttask_status")]

    operations = [
        migrations.AddField(
            model_name="onecmonthlyprofit", name="calculated_cost",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=20, null=True, verbose_name="Расчётная себестоимость"),
        ),
        migrations.AddField(
            model_name="onecmonthlyprofit", name="cost_source",
            field=models.CharField(blank=True, choices=[("actual", "Фактическая"), ("calculated", "Расчётная"), ("undefined", "Не определена")], default="", max_length=16, verbose_name="Источник себестоимости"),
        ),
        migrations.AddField(
            model_name="onecmonthlyprofit", name="cost_calculation_method",
            field=models.CharField(blank=True, default="", max_length=40, verbose_name="Метод расчёта себестоимости"),
        ),
        migrations.AddField(
            model_name="onecmonthlyprofit", name="cost_calculation_ratio",
            field=models.DecimalField(blank=True, decimal_places=10, max_digits=16, null=True, verbose_name="Коэффициент расчёта себестоимости"),
        ),
        migrations.AddField(
            model_name="onecmonthlyprofit", name="analytical_gross_profit",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=20, null=True, verbose_name="Аналитическая валовая прибыль"),
        ),
    ]
