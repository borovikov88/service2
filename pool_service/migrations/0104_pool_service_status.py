from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_service_status(apps, schema_editor):
    Pool = apps.get_model("pool_service", "Pool")
    Pool.objects.filter(service_suspended=True).update(service_status="paused")
    Pool.objects.filter(service_suspended=False).update(service_status="active")


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
                    ("paused", "Обслуживание приостановлено"),
                    ("winterized", "Законсервирован"),
                    ("stopped", "Больше не обслуживаем"),
                ],
                default="active",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_service_status, migrations.RunPython.noop),
        migrations.CreateModel(
            name="PoolServiceStatusEvent",
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
                    "previous_status",
                    models.CharField(
                        choices=[
                            ("active", "На обслуживании"),
                            ("paused", "Обслуживание приостановлено"),
                            ("winterized", "Законсервирован"),
                            ("stopped", "Больше не обслуживаем"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("active", "На обслуживании"),
                            ("paused", "Обслуживание приостановлено"),
                            ("winterized", "Законсервирован"),
                            ("stopped", "Больше не обслуживаем"),
                        ],
                        max_length=20,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "changed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pool_service_status_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "pool",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="service_status_events",
                        to="pool_service.pool",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["pool", "created_at"],
                        name="pool_status_event_idx",
                    ),
                ],
            },
        ),
    ]
