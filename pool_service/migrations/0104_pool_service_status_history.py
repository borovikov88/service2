from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_service_status(apps, schema_editor):
    Pool = apps.get_model("pool_service", "Pool")
    Pool.objects.filter(service_suspended=True).update(service_status="suspended")
    Pool.objects.filter(service_suspended=False).update(service_status="active")


def reverse_service_status(apps, schema_editor):
    Pool = apps.get_model("pool_service", "Pool")
    Pool.objects.exclude(service_status="active").update(service_suspended=True)
    Pool.objects.filter(service_status="active").update(service_suspended=False)


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("pool_service", "0103_onec_diagnostic_mcp_audit"),
    ]

    operations = [
        migrations.AddField(
            model_name="pool",
            name="service_status",
            field=models.CharField(
                choices=[
                    ("active", "На обслуживании"),
                    ("suspended", "Обслуживание приостановлено"),
                    ("winterized", "Законсервирован"),
                    ("stopped", "Больше не обслуживаем"),
                ],
                default="active",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_service_status, reverse_service_status),
        migrations.CreateModel(
            name="PoolStatusHistory",
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
                (
                    "old_status",
                    models.CharField(
                        choices=[
                            ("active", "На обслуживании"),
                            ("suspended", "Обслуживание приостановлено"),
                            ("winterized", "Законсервирован"),
                            ("stopped", "Больше не обслуживаем"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "new_status",
                    models.CharField(
                        choices=[
                            ("active", "На обслуживании"),
                            ("suspended", "Обслуживание приостановлено"),
                            ("winterized", "Законсервирован"),
                            ("stopped", "Больше не обслуживаем"),
                        ],
                        max_length=20,
                    ),
                ),
                ("comment", models.CharField(blank=True, max_length=255)),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                (
                    "changed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pool_status_changes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "pool",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="status_history",
                        to="pool_service.pool",
                    ),
                ),
            ],
            options={
                "ordering": ["-changed_at", "-id"],
            },
        ),
        migrations.AddIndex(
            model_name="poolstatushistory",
            index=models.Index(
                fields=["pool", "changed_at"],
                name="pool_status_hist_idx",
            ),
        ),
    ]
