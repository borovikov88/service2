"""Persistent metadata-only audit events for the 1C Diagnostic MCP."""

from django.conf import settings
from django.db import models

from pool_service.models import FinanceMcpGrant, FinanceMcpPrincipal, Organization


class OneCDiagnosticMcpAuditEvent(models.Model):
    RESULT_SUCCESS = "success"
    RESULT_DENIED = "denied"
    RESULT_ERROR = "error"
    RESULT_CHOICES = [
        (RESULT_SUCCESS, "Успех"),
        (RESULT_DENIED, "Отклонено"),
        (RESULT_ERROR, "Ошибка"),
    ]

    principal = models.ForeignKey(
        FinanceMcpPrincipal,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="onec_diagnostic_audit_events",
    )
    grant = models.ForeignKey(
        FinanceMcpGrant,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="onec_diagnostic_audit_events",
    )
    authorized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="authorized_onec_diagnostic_audit_events",
    )
    target_organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="onec_diagnostic_audit_events",
    )
    tool_name = models.CharField(max_length=100)
    entity_set = models.CharField(max_length=300, blank=True)
    # Field names are metadata, never values. Values of filters and rows are
    # deliberately absent from this model.
    selected_fields = models.JSONField(default=list, blank=True)
    result = models.CharField(max_length=16, choices=RESULT_CHOICES)
    duration_ms = models.PositiveIntegerField(default=0)
    response_bytes = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "pool_service"
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(
                fields=["principal", "created_at"],
                name="onec_diag_audit_principal_idx",
            ),
            models.Index(
                fields=["tool_name", "created_at"],
                name="onec_diag_audit_tool_idx",
            ),
        ]

    def __str__(self):
        at = self.created_at.isoformat(timespec="seconds") if self.created_at else "pending"
        return (
            f"1C Diagnostic MCP principal={self.principal_id or 'unknown'} "
            f"tool={self.tool_name} at={at}"
        )
