from django.conf import settings
from django.db import models


class CustomerReminderPolicy(models.Model):
    organization = models.OneToOneField("pool_service.Organization", on_delete=models.CASCADE)
    # Keep former per-manager settings for existing installations.
    duty_manager = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", null=True, blank=True, editable=False,
    )
    fallback_owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+", db_column="supervisor_id",
    )
    started_at = models.DateTimeField(null=True, blank=True)
    last_tick_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_digest_on = models.DateField(null=True, blank=True, editable=False)


class MissedCallFollowup(models.Model):
    call = models.OneToOneField("pool_service.PhoneCall", on_delete=models.CASCADE)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+",
    )
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=250, blank=True)


class CustomerReminderReceipt(models.Model):
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.CASCADE)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    work_key = models.CharField(max_length=80)
    step = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user", "work_key", "step"],
                name="customer_reminder_work_step",
            ),
        ]

