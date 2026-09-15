from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("pool_service", "0102_finance_position_snapshot"),
    ]

    operations = [
        migrations.CreateModel(
            name="OneCDiagnosticMcpAuditEvent",
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
                ("tool_name", models.CharField(max_length=100)),
                ("entity_set", models.CharField(blank=True, max_length=300)),
                ("selected_fields", models.JSONField(blank=True, default=list)),
                (
                    "result",
                    models.CharField(
                        choices=[
                            ("success", "Успех"),
                            ("denied", "Отклонено"),
                            ("error", "Ошибка"),
                        ],
                        max_length=16,
                    ),
                ),
                ("duration_ms", models.PositiveIntegerField(default=0)),
                ("response_bytes", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "authorized_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="authorized_onec_diagnostic_audit_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "grant",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="onec_diagnostic_audit_events",
                        to="pool_service.financemcpgrant",
                    ),
                ),
                (
                    "principal",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="onec_diagnostic_audit_events",
                        to="pool_service.financemcpprincipal",
                    ),
                ),
                (
                    "target_organization",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="onec_diagnostic_audit_events",
                        to="pool_service.organization",
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["principal", "created_at"],
                        name="onec_diag_audit_principal_idx",
                    ),
                    models.Index(
                        fields=["tool_name", "created_at"],
                        name="onec_diag_audit_tool_idx",
                    ),
                ],
            },
        ),
    ]
