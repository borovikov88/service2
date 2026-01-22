from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0035_client_access_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="pool",
            name="service_interval_days",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="pool",
            name="daily_readings_required",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="Notification",
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
                ("kind", models.CharField(choices=[("limits", "limits"), ("missed_visit", "missed_visit"), ("daily_missing", "daily_missing"), ("new_company", "new_company"), ("new_personal", "new_personal")], max_length=32)),
                ("level", models.CharField(choices=[("info", "info"), ("warning", "warning"), ("critical", "critical")], default="info", max_length=16)),
                ("title", models.CharField(max_length=200)),
                ("message", models.TextField(blank=True)),
                ("action_url", models.CharField(blank=True, max_length=400)),
                ("dedupe_key", models.CharField(blank=True, db_index=True, max_length=120)),
                ("is_read", models.BooleanField(default=False)),
                ("is_resolved", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "client",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to="pool_service.client"),
                ),
                (
                    "organization",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to="pool_service.organization"),
                ),
                (
                    "pool",
                    models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to="pool_service.pool"),
                ),
                (
                    "user",
                    models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="notifications", to="auth.user"),
                ),
            ],
            options={
                "indexes": [
                    models.Index(fields=["user", "is_read"], name="notif_user_read_idx"),
                    models.Index(fields=["user", "is_resolved"], name="notif_user_resolved_idx"),
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="notification",
            constraint=models.UniqueConstraint(condition=~models.Q(dedupe_key=""), fields=("user", "dedupe_key"), name="unique_notification_dedupe"),
        ),
    ]
