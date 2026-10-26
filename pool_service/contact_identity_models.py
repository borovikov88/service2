"""Conversation-local provenance and clarification, not a second client register.

Validated ORM contract; raw SQL/historical migration models remain trusted paths.
No message body or provider payload is accepted here.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q

from .crm_commercial_models import CommercialModel, CommercialQuerySet


class ProvenanceQuerySet(CommercialQuerySet):
    def delete(self):
        raise ValidationError("Contact provenance cannot be deleted through this queryset.")


class ConversationProvenance(CommercialModel):
    objects = ProvenanceQuerySet.as_manager()
    organization = models.ForeignKey("pool_service.Organization", on_delete=models.PROTECT)
    conversation = models.ForeignKey("pool_service.Conversation", on_delete=models.CASCADE)
    # Immutable source snapshots survive provider rebinding and message deletion.
    connection_id_snapshot = models.PositiveBigIntegerField()
    account_id_snapshot = models.CharField(max_length=255, blank=True)
    source_kind = models.CharField(max_length=24)
    conversation_external_id = models.CharField(max_length=255)

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):
        raise ValidationError("Contact provenance cannot be deleted through this model.")

    def clean(self):
        super().clean()
        from .communication_models import Conversation
        conversation = Conversation.objects.select_related("connection__channel").get(pk=self.conversation_id)
        if (conversation.organization_id != self.organization_id
                or conversation.connection.channel.organization_id != self.organization_id
                or self.connection_id_snapshot != conversation.connection_id
                or self.account_id_snapshot != str(conversation.connection.external_id)
                or self.source_kind != conversation.connection.channel.kind
                or self.conversation_external_id != conversation.external_id):
            raise ValidationError("Contact source scope is inconsistent.")


class ContactObservation(ConversationProvenance):
    """Immutable evidence; provider supplied does not mean human confirmed."""
    event_key = models.CharField(max_length=64)
    source_message_id = models.PositiveBigIntegerField(null=True, blank=True)
    origin = models.CharField(max_length=24, choices=[(v, v) for v in (
        "provider_identity", "provider_phone", "message", "resolution")])
    kind = models.CharField(max_length=16, choices=[(v, v) for v in ("identity", "phone", "email")])
    value = models.CharField(max_length=255)
    role = models.CharField(max_length=16, choices=[("unknown", "Unknown"), ("foreman", "Foreman")], default="unknown")
    confirmation = models.CharField(max_length=16, choices=[(v, v) for v in ("suggested", "provider", "confirmed")])
    confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    immutable_scope = ("organization_id", "conversation_id", "connection_id_snapshot", "account_id_snapshot", "source_kind",
        "conversation_external_id", "event_key", "source_message_id", "origin", "kind", "value", "role",
        "confirmation", "confirmed_by_id")

    class Meta:
        base_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(fields=["conversation", "event_key"], name="contact_observation_event"),
            models.CheckConstraint(condition=(Q(confirmation="confirmed", confirmed_by__isnull=False, origin="resolution")
                | Q(confirmation="suggested", confirmed_by__isnull=True, origin="message")
                | Q(confirmation="provider", confirmed_by__isnull=True, origin__in=["provider_identity", "provider_phone"])),
                name="contact_observation_confirmation"),
        ]

    def save(self, *args, **kwargs):
        if self.pk and type(self).objects.filter(pk=self.pk).exists():
            raise ValidationError("Contact observations are immutable.")
        return super().save(*args, **kwargs)

    def clean(self):
        super().clean()
        from .communication_models import ConversationMessage
        if self.origin == "message" and (not self.source_message_id or not ConversationMessage.objects.filter(
                pk=self.source_message_id, conversation_id=self.conversation_id, direction="in").exists()):
            raise ValidationError("Message evidence requires its incoming source event.")
        if self.origin == "provider_identity" and self.kind != "identity" or self.origin == "provider_phone" and self.kind != "phone":
            raise ValidationError("Provider observation kind is inconsistent.")
        if self.origin == "message" and self.kind not in {"phone", "email"}:
            raise ValidationError("Only suggested contacts may come from a message.")
        if self.role != "unknown" and (self.origin != "message" or self.kind != "phone"):
            raise ValidationError("Contact roles require a suggested phone.")
        if self.kind == "phone":
            from .phone_utils import normalize_phone
            if normalize_phone(self.value) != self.value:
                raise ValidationError("Observation phone must be normalized.")
        if self.origin == "resolution" and (self.kind != "identity" or not self.value.isascii() or not self.value.isdigit()):
            raise ValidationError("Resolution evidence requires a client identity.")
        if self.origin != "message" and self.source_message_id is not None:
            raise ValidationError("Only message evidence may refer to a message.")

    def delete(self, *args, **kwargs):
        raise ValidationError("Contact observations are immutable.")


class ContactIdentityReview(ConversationProvenance):
    conversation = models.OneToOneField("pool_service.Conversation", on_delete=models.CASCADE, related_name="identity_review")
    candidate_ids = models.JSONField(default=list, blank=True)
    candidates_truncated = models.BooleanField(default=False)
    reason = models.CharField(max_length=32, choices=[("provider_phone", "Provider phone"), ("provider_identity", "Provider identity"), ("candidate_drift", "Candidate drift")])
    revision = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=12, choices=[("pending", "Pending"), ("resolved", "Resolved")], default="pending")
    # Historical decision IDs, not live Client FKs: generic merge must not rewrite an audit decision.
    selected_client_id = models.PositiveBigIntegerField(null=True, blank=True)
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    operation_key = models.CharField(max_length=64, blank=True)
    request_revision = models.PositiveIntegerField(null=True, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    immutable_scope = ("organization_id", "conversation_id", "connection_id_snapshot", "account_id_snapshot", "source_kind", "conversation_external_id", "candidate_ids", "candidates_truncated", "reason")

    class Meta:
        base_manager_name = "objects"
        constraints = [models.CheckConstraint(condition=(
            Q(state="pending", selected_client_id__isnull=True, resolved_by__isnull=True,
              resolved_at__isnull=True, operation_key="", request_revision__isnull=True)
            | (Q(state="resolved", selected_client_id__isnull=False, resolved_by__isnull=False,
                 resolved_at__isnull=False, request_revision__isnull=False) & ~Q(operation_key=""))), name="contact_review_resolution")]

    def clean(self):
        super().clean()
        from .models import Client
        ids = self.candidate_ids
        if (type(ids) is not list or not (0 if self.reason == "candidate_drift" else 2) <= len(ids) <= 50 or any(type(v) is not int or v <= 0 for v in ids)
                or len(set(ids)) != len(ids)):
            raise ValidationError("Contact review requires bounded distinct candidate IDs.")
        if Client.objects.filter(pk__in=ids, organization_id=self.organization_id).count() != len(ids):
            raise ValidationError("Contact review candidates must belong to the organization.")
        old = type(self).objects.filter(pk=self.pk).first() if self.pk else None
        if old and old.state == "resolved":
            fields = ("state", "selected_client_id", "resolved_by_id", "operation_key", "request_revision", "resolved_at", "revision")
            if any(getattr(old, name) != getattr(self, name) for name in fields):
                raise ValidationError("A contact identity decision is immutable.")
        if self.selected_client_id is not None and self.selected_client_id not in ids:
            raise ValidationError("Resolution must choose a recorded candidate.")
