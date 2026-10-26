"""Durable source locators and delivery state; never communication content."""
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone


class CrmInboxBoundary(models.Model):
    key = models.CharField(max_length=24, primary_key=True, default="sources")
    captured_at = models.DateTimeField(default=timezone.now)


class InboxQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Use validated inbox instance.save().")

    def bulk_create(self, objs, **kwargs):
        raise ValidationError("Bulk inbox writes are unavailable.")

    def bulk_update(self, objs, fields, **kwargs):
        raise ValidationError("Bulk inbox writes are unavailable.")

    def delete(self):
        raise ValidationError("Inbox evidence cannot be deleted through the supported ORM.")


class CrmInboxEvent(models.Model):
    objects = InboxQuerySet.as_manager()
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    source_type = models.CharField(max_length=8, choices=[("message", "Message"), ("call", "Call")])
    source_id = models.PositiveBigIntegerField()
    source_key = models.CharField(max_length=64)
    provider_kind = models.CharField(max_length=24)
    connection_id_snapshot = models.PositiveBigIntegerField()
    channel_id_snapshot = models.PositiveBigIntegerField()
    account_id_snapshot = models.CharField(max_length=255, blank=True)
    namespace = models.CharField(max_length=255, blank=True)
    external_id = models.CharField(max_length=255, blank=True)
    scope_verified = models.BooleanField(default=False)
    revision = models.PositiveIntegerField(default=1)
    fingerprint = models.CharField(max_length=64)
    occurred_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField()
    origin = models.CharField(max_length=16)
    historical = models.BooleanField(default=True)
    state = models.CharField(max_length=20, default="pending", choices=[(v, v) for v in (
        "pending", "claimed", "awaiting_consumer", "retry", "failed", "blocked", "done", "superseded")])
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_until = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    prepared_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    immutable_fields = ("organization_id", "source_type", "source_id", "source_key", "provider_kind",
        "connection_id_snapshot", "channel_id_snapshot", "account_id_snapshot", "namespace", "external_id",
        "scope_verified", "revision", "fingerprint", "occurred_at", "received_at", "origin", "historical")

    class Meta:
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(fields=["source_key", "revision"], name="crm_inbox_key_revision"),
            models.UniqueConstraint(fields=["source_type", "source_id", "revision"], name="crm_inbox_locator_revision"),
            models.CheckConstraint(condition=Q(revision__gte=1), name="crm_inbox_revision_positive"),
            models.CheckConstraint(condition=(Q(state="claimed", lease_token__isnull=False, lease_until__isnull=False)
                | (~Q(state="claimed") & Q(lease_token__isnull=True, lease_until__isnull=True))), name="crm_inbox_claim_shape"),
            models.CheckConstraint(condition=(Q(state="done", processed_at__isnull=False)
                | (~Q(state="done") & Q(processed_at__isnull=True))), name="crm_inbox_done_shape"),
            models.CheckConstraint(condition=Q(source_type__in=["message", "call"]), name="crm_inbox_source_type"),
            models.CheckConstraint(condition=Q(state__in=["pending", "claimed", "awaiting_consumer", "retry", "failed", "blocked", "done", "superseded"]), name="crm_inbox_state"),
        ]
        indexes = [models.Index(fields=["organization", "state", "next_attempt_at"], name="crm_inbox_due_idx"),
            models.Index(fields=["source_type", "source_id"], name="crm_inbox_source_idx")]

    def save(self, *args, **kwargs):
        previous = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if previous and any(getattr(previous, name) != getattr(self, name) for name in self.immutable_fields):
            raise ValidationError("Inbox source snapshots are immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Inbox evidence cannot be deleted through the supported ORM.")

    def clean(self):
        super().clean()
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            return  # Historical locators survive deletion/rebinding; delivery blocks them.
        from .communication_models import ConversationMessage, PhoneCall
        if self.source_type == "message":
            source = ConversationMessage.objects.select_related("conversation").filter(pk=self.source_id).first()
            org_id = source.conversation.organization_id if source else None
        else:
            source = PhoneCall.objects.filter(pk=self.source_id).first()
            org_id = source.organization_id if source else None
        if source is None or org_id != self.organization_id:
            raise ValidationError("Inbox source scope is inconsistent.")
        if self.scope_verified:
            scope = source.intake_scope or {}
            expected = (self.organization_id, self.provider_kind, self.connection_id_snapshot,
                self.channel_id_snapshot, self.account_id_snapshot, self.namespace, self.external_id)
            actual = tuple(scope.get(name) for name in ("organization", "provider", "connection", "channel", "account", "namespace", "external"))
            if actual != expected:
                raise ValidationError("Inbox source snapshot is inconsistent.")
        elif not self.historical:
            raise ValidationError("Unverified legacy sources must stay historical.")


class CrmInboxCursor(models.Model):
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    source_type = models.CharField(max_length=8)
    connection_id_snapshot = models.PositiveBigIntegerField()
    account_id_snapshot = models.CharField(max_length=255)
    # A finite cycle revisits same-PK mutations. Each row/cursor commit is atomic.
    after_id = models.PositiveBigIntegerField(default=0)
    requested_through_id = models.PositiveBigIntegerField(default=0)
    through_id = models.PositiveBigIntegerField(default=0)
    last_id = models.PositiveBigIntegerField(default=0)
    include_legacy = models.BooleanField(default=False)
    cycles = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)
    error_code = models.CharField(max_length=40, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "source_type", "connection_id_snapshot", "account_id_snapshot", "include_legacy", "after_id", "requested_through_id"], name="crm_inbox_cursor_scope")]
