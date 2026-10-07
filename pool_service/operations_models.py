"""Indexed delivery scheduling; delivery/idempotency history stays on the task."""

from django.db import models
from django.utils import timezone


class OperationsPushQueue(models.Model):
    task = models.OneToOneField(
        "pool_service.ServiceTask",
        on_delete=models.CASCADE,
        primary_key=True,
        related_name="operations_push_queue_entry",
    )
    next_attempt_at = models.DateTimeField(default=timezone.now)
    last_marker = models.CharField(max_length=160, blank=True, default="")

    class Meta:
        indexes = [
            models.Index(fields=["next_attempt_at", "task"], name="ops_push_due_task_idx"),
        ]
