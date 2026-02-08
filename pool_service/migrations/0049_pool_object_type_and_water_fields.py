from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0048_servicevisitplan"),
    ]

    operations = [
        migrations.AddField(
            model_name="pool",
            name="object_type",
            field=models.CharField(
                choices=[("pool", "Бассейн"), ("water", "Водоподготовка")],
                default="pool",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="pool",
            name="water_system_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("softening", "Умягчение"),
                    ("iron_removal", "Обезжелезивание"),
                    ("reverse_osmosis", "Обратный осмос"),
                    ("uv", "УФ обработка"),
                    ("complex", "Комплексная"),
                ],
                max_length=30,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="pool",
            name="water_source",
            field=models.CharField(
                blank=True,
                choices=[("well", "Скважина"), ("city", "Центральная"), ("tank", "Резервуар")],
                max_length=30,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="pool",
            name="water_capacity_value",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pool",
            name="water_capacity_unit",
            field=models.CharField(
                blank=True,
                choices=[("m3_hour", "м³/час"), ("m3_day", "м³/сутки")],
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="pool",
            name="water_control_parameters",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pool",
            name="water_equipment",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pool",
            name="water_operation_mode",
            field=models.CharField(
                blank=True,
                choices=[("continuous", "Постоянно"), ("scheduled", "По расписанию")],
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="pool",
            name="water_contact_name",
            field=models.CharField(blank=True, max_length=120, null=True),
        ),
        migrations.AddField(
            model_name="pool",
            name="water_contact_phone",
            field=models.CharField(blank=True, max_length=30, null=True),
        ),
        migrations.AddField(
            model_name="pool",
            name="water_access_notes",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="waterreading",
            name="consumables_replaced",
            field=models.TextField(blank=True, null=True),
        ),
    ]
