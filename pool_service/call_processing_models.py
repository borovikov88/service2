"""Draft call-processing preferences. Saving these records never starts AI.

No admin registration: personal numbers are exposed only through their owner's
settings service. A reviewed worker/visibility/budget integration is still needed.
"""
from django.conf import settings
from django.db import models


MODE_CHOICES = [
    ("manual", "Only manually"),
    ("all_except", "All except personal"),
    ("allowlist", "Only rules"),
]


class CallProcessingRule(models.Model):
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.CASCADE)
    employee = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="call_processing_rules")
    mode = models.CharField(max_length=16, choices=MODE_CHOICES, default="manual")
    include_staff = models.BooleanField(default=True)
    work_numbers = models.JSONField(default=list, blank=True)
    # None means prepared, not activated. A later activation must establish a
    # new-call boundary; saving or previewing does not backfill any archive.
    effective_from = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveIntegerField(default=0)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="changed_call_processing_rules")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "employee"], name="call_rule_org_employee_uniq")]


class CallPrivateNumber(models.Model):
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.CASCADE)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="private_call_numbers")
    label = models.CharField(max_length=120)
    phone_key = models.CharField(max_length=16)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "owner", "phone_key"], name="call_private_owner_phone_uniq")]


class CallProcessingRuleAudit(models.Model):
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.CASCADE)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="call_processing_audits")
    target_user_id = models.PositiveBigIntegerField()
    action = models.CharField(max_length=32)
    # Counts and policy mode only. Never copy private labels/phones into logs.
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)


class CallProcessingBudget(models.Model):
    organization = models.OneToOneField(
        "pool_service.Organization",
        on_delete=models.CASCADE,
        related_name="call_processing_budget",
    )
    monthly_limit_usd = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True
    )
    revision = models.PositiveIntegerField(default=0)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="changed_call_processing_budgets",
    )
    updated_at = models.DateTimeField(auto_now=True)


class CallProcessingUsage(models.Model):
    STAGE_TRANSCRIPTION = "transcription"
    STAGE_ANALYSIS = "analysis"
    STAGE_CHOICES = [
        (STAGE_TRANSCRIPTION, "Transcription"),
        (STAGE_ANALYSIS, "Analysis"),
    ]

    STATUS_RESERVED = "reserved"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_RELEASED = "released"
    STATUS_CHOICES = [
        (STATUS_RESERVED, "Reserved"),
        (STATUS_SUCCEEDED, "Succeeded"),
        (STATUS_FAILED, "Failed"),
        (STATUS_RELEASED, "Released"),
    ]

    organization = models.ForeignKey(
        "pool_service.Organization",
        on_delete=models.CASCADE,
        related_name="call_processing_usage",
    )
    call = models.ForeignKey(
        "pool_service.PhoneCall",
        on_delete=models.CASCADE,
        related_name="processing_usage",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="call_processing_usage",
    )
    attempt_key = models.CharField(max_length=64)
    stage = models.CharField(max_length=16, choices=STAGE_CHOICES)
    model = models.CharField(max_length=80)
    status = models.CharField(
        max_length=16, choices=STATUS_CHOICES, default=STATUS_RESERVED
    )
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    input_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    output_tokens = models.PositiveBigIntegerField(null=True, blank=True)
    reserved_cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True
    )
    estimated_cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True
    )
    usage_cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True
    )
    confirmed_cost_usd = models.DecimalField(
        max_digits=12, decimal_places=6, null=True, blank=True
    )
    tariff_version = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["call", "attempt_key", "stage"],
                name="call_usage_attempt_stage_uniq",
            ),
        ]
        indexes = [
            models.Index(
                fields=["organization", "created_at"],
                name="call_usage_org_created_idx",
            ),
            models.Index(
                fields=["organization", "status", "created_at"],
                name="call_usage_org_status_idx",
            ),
        ]
