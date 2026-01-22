from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0036_notifications"),
    ]

    operations = [
        migrations.CreateModel(
            name="OrganizationWaterNorms",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("ph_min", models.FloatField(blank=True, null=True)),
                ("ph_max", models.FloatField(blank=True, null=True)),
                ("cl_free_min", models.FloatField(blank=True, null=True)),
                ("cl_free_max", models.FloatField(blank=True, null=True)),
                ("cl_total_min", models.FloatField(blank=True, null=True)),
                ("cl_total_max", models.FloatField(blank=True, null=True)),
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="water_norms",
                        to="pool_service.organization",
                    ),
                ),
            ],
        ),
    ]
