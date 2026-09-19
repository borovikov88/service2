from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_service_status(apps, schema_editor):
    Pool = apps.get_model("pool_service", "Pool")
    Pool.objects.filter(service_suspended=True).update(service_status="paused")
    Pool.objects.filter(service_suspended=False).update(service_status="active")


def restore_service_suspended(apps, schema_editor):
    Pool = apps.get_model("pool_service", "Pool")
    Pool.objects.filter(service_status="active").update(service_suspended=False)
    Pool.objects.exclude(service_status="active").update(service_suspended=True)


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
                    ("conserved", "Законсервирован"),
                    ("ended", "Больше не обслуживаем"),
                ],
                default="active",
                max_length=20,
            ),
        ),
        migrations.RunPython(backfill_service_status, restore_service_suspended),
        migrations.CreateModel(
            name="PoolServiceStatusChange",
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
                            ("conserved", "Законсервирован"),
                            ("ended", "Больше не обслуживаем"),
                        ],
                        max_length=20,
                    ),
                ),
                (
                    "new_status",
                    models.CharField(
                        choices=[
                            ("active", "На обслуживании"),
                            ("paused", "Обслуживание приостановлено"),
                            ("conserved", "Законсервирован"),
                            ("ended", "Больше не обслуживаем"),
                        ],
                        max_length=20,
                    ),
                ),
                ("comment", models.CharField(blank=True, max_length=255)),
                ("changed_at", models.DateTimeField()),
                (
                    "changed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="pool_service_status_changes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "pool",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="service_status_changes",
                        to="pool_service.pool",
                    ),
                ),
            ],
            options={
                "ordering": ["-changed_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["pool", "changed_at"],
                        name="pool_status_changed_idx",
                    ),
                ],
            },
        ),
    ]
