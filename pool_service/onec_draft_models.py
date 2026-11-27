"""Private review records and persistent idempotency claims for writes in Fresh."""
import uuid

from django.conf import settings
from django.db import models


class OneCDraftRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.CASCADE, related_name="+")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    token_hash = models.CharField(max_length=64, unique=True, null=True, editable=False)
    kind = models.CharField(max_length=16, choices=[("document", "Документ"), ("nomenclature", "Номенклатура")])
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    intent = models.JSONField(null=True, editable=False)
    document_snapshot = models.JSONField(null=True, editable=False)
    submitted_at = models.DateTimeField(null=True, editable=False)
    approved_plan = models.CharField(max_length=64, blank=True, editable=False)
    card_plan = models.JSONField(default=list, editable=False)
    selections = models.JSONField(default=dict, editable=False)
    approved_at = models.DateTimeField(null=True, editable=False)
    approved_digest = models.CharField(max_length=64, blank=True, editable=False)
    approved_binding = models.CharField(max_length=64, blank=True, editable=False)

    class Meta:
        indexes = [
            models.Index(fields=["organization", "user", "created_at"], name="onec_req_user_time"),
        ]


class OneCWriteFence(models.Model):
    digest = models.CharField(max_length=64, primary_key=True, editable=False)
    request = models.ForeignKey(OneCDraftRequest, on_delete=models.PROTECT, related_name="write_attempts")
    remote_key = models.UUIDField(editable=False)
    reserved_at = models.DateTimeField(auto_now_add=True, db_index=True)
    mismatch = models.BooleanField(default=False, editable=False)
    is_existing = models.BooleanField(default=False, editable=False)
    author_was_empty = models.BooleanField(null=True, editable=False)
    verified_at = models.DateTimeField(null=True, editable=False)
