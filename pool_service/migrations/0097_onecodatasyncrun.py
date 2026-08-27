import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("pool_service", "0096_cashflow_odata_source_identity"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="OneCODataSyncRun",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("pending", "Ожидает запуска"), ("running", "Выполняется"), ("completed", "Завершена"), ("partial_failed", "Завершена частично"), ("failed", "Ошибка"), ("cancelled", "Отменена")], default="pending", max_length=20)),
                ("requested_report_types", models.JSONField(default=list)),
                ("sync_scope", models.JSONField(default=dict)),
                ("cursor", models.JSONField(default=dict)),
                ("progress", models.JSONField(default=dict)),
                ("result_summary", models.JSONField(default=dict)),
                ("lease_token", models.UUIDField(blank=True, editable=False, null=True)),
                ("lease_report_type", models.CharField(blank=True, max_length=30)),
                ("lease_chunk", models.JSONField(blank=True, default=dict)),
                ("lease_started_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("error_message", models.TextField(blank=True)),
                ("organization", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="onec_odata_sync_runs", to="pool_service.organization")),
                ("requested_by", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="requested_onec_odata_sync_runs", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddIndex(
            model_name="onecodatasyncrun",
            index=models.Index(fields=["organization", "status", "created_at"], name="onec_sync_org_status_idx"),
        ),
    ]
