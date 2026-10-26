"""Internal, bounded contact clarification. No HTTP, grants, merge or outbound I/O."""
import hashlib
import json
import re

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .client_queries import active_clients
from .communication_models import ChannelConnection, CommunicationAccess, Conversation
from .contact_identity_models import ContactIdentityReview, ContactObservation
from .crm_commercial_models import CrmAccess
from .models import Client, Employee, Organization, OrganizationAccess
from .services.permissions import company_has_access


class IdentityConflict(ValidationError):
    pass


def source_fields(conversation):
    return dict(organization_id=conversation.organization_id, conversation=conversation,
        connection_id_snapshot=conversation.connection_id, account_id_snapshot=str(conversation.connection.external_id), source_kind=conversation.connection.channel.kind,
        conversation_external_id=conversation.external_id)


def observe(conversation, *, origin, kind, value, message_id=None, role="unknown", actor=None):
    key = hashlib.sha256(json.dumps([origin, kind, value, message_id, role], ensure_ascii=True).encode()).hexdigest()
    return ContactObservation.objects.get_or_create(conversation=conversation, event_key=key, defaults={
        **source_fields(conversation), "origin": origin, "kind": kind, "value": value,
        "source_message_id": message_id, "role": role,
        "confirmation": "confirmed" if actor else "suggested" if origin == "message" else "provider",
        "confirmed_by": actor,
    })[0]


def suggested_role(body, value):
    # Require a short, direct label-number assertion. Negation, another owner's
    # claim or intervening prose is uncertainty, not evidence of a foreman role.
    from .communication_contact_data import extract_contacts
    direct = r"\s*(?:вот\s+)?(?:телефон|номер)\s+прораба\s*[:—-]?\s*\+?\d[\d() \t-]{7,28}\d\s*"
    for clause in re.split(r"[\n;.!?]", body[:10000]):
        if len(clause) <= 80 and re.fullmatch(direct, clause, re.I):
            if extract_contacts(clause)["phones"] == [value]:
                return "foreman"
    return "unknown"


def queue_review(conversation, ids, reason, *, candidates_truncated=False):
    ordered = sorted(set(ids))
    # Historical IDs are evidence, not current candidate authority. Drift must
    # retain this new source event without propagating foreign/merged/deleted IDs
    # or falling through to automatic Client creation. Lock retained candidates
    # until the validated new snapshot has been saved.
    current = list(active_clients(Client.objects.select_for_update().filter(
        pk__in=ordered, organization_id=conversation.organization_id)).order_by("pk").values_list("pk", flat=True))
    if current != ordered:
        reason = "candidate_drift"
    return ContactIdentityReview.objects.get_or_create(conversation=conversation, defaults={
        **source_fields(conversation), "candidate_ids": current[:50],
        "candidates_truncated": candidates_truncated or len(ordered) > 50, "reason": reason,
    })[0]


def _locked_scope(actor, organization_id, conversation_id):
    # Match the inbound/enrichment order: connection -> organization -> conversation.
    ref = Conversation.objects.filter(pk=conversation_id, organization_id=organization_id).values("connection_id").first()
    if ref is None:
        raise PermissionDenied
    connection = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=ref["connection_id"])
    organization = Organization.objects.select_for_update().get(pk=organization_id)
    conversation = Conversation.objects.select_for_update().filter(pk=conversation_id, organization_id=organization_id).first()
    user = get_user_model().objects.select_for_update().filter(pk=getattr(actor, "pk", None)).first()
    memberships = list(OrganizationAccess.objects.select_for_update().filter(
        organization=organization, user=user).order_by("pk")) if user else []
    employees = list(Employee.objects.select_for_update().filter(organization=organization, user=user).order_by("pk")) if user else []
    grant = CrmAccess.objects.select_for_update().filter(organization=organization, user=user).first() if user else None
    communications = CommunicationAccess.objects.select_for_update().filter(organization=organization, user=user).first() if user else None
    if (not user or not user.is_active or user.is_superuser
            or not any(m.role in {"owner", "admin"} for m in memberships)
            or any(not e.is_active or e.employment_status == Employee.STATUS_DISMISSED for e in employees)
            or not grant or not grant.resolve_contact_identity
            or not communications or not communications.can_view_conversations or not communications.can_take_conversation
            or not company_has_access(organization)
            or not conversation or conversation.connection_id != connection.pk
            or connection.channel.organization_id != organization.pk
            or not connection.is_active or not connection.channel.is_active
            or conversation.assignee_id not in (None, user.pk) and not communications.can_assign_conversation):
        raise PermissionDenied
    conversation.connection = connection
    if conversation.client_id:
        current_client = Client.objects.select_for_update().filter(pk=conversation.client_id, organization_id=organization_id).first()
        if current_client is None:
            raise PermissionDenied
    return user, conversation


def _review(conversation):
    review = ContactIdentityReview.objects.select_for_update().filter(conversation=conversation).first()
    if review is None:
        raise PermissionDenied
    # Also refuses stored cross-org/rebound source graphs, before returning IDs.
    review.full_clean()
    if review.state == "resolved" and conversation.client_id != review.selected_client_id:
        raise IdentityConflict("The current source binding differs from its historical resolution.")
    return review


@transaction.atomic
def preview_identity_review(*, actor, organization_id, conversation_id):
    _, conversation = _locked_scope(actor, organization_id, conversation_id)
    review = _review(conversation)
    candidates = active_clients(Client.objects.select_for_update().filter(
        pk__in=review.candidate_ids, organization_id=organization_id)).order_by("pk")
    # Minimal identity preview; no contacts, source text, deals or other private sources.
    return {"review_id": review.pk, "revision": review.revision, "state": review.state,
        "candidate_ids": list(candidates.values_list("pk", flat=True)),
        "candidates_truncated": review.candidates_truncated, "reason": review.reason}


@transaction.atomic
def resolve_identity_review(*, actor, organization_id, conversation_id, client_id, expected_revision, operation_key):
    if (type(client_id) is not int or client_id <= 0 or type(expected_revision) is not int or expected_revision <= 0
            or type(operation_key) is not str or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", operation_key)):
        raise ValidationError("Invalid identity resolution request.")
    user, conversation = _locked_scope(actor, organization_id, conversation_id)
    review = _review(conversation)
    client = active_clients(Client.objects.select_for_update().filter(pk=client_id, organization_id=organization_id)).first()
    if client is None or client_id not in review.candidate_ids:
        raise PermissionDenied
    if review.state == "resolved":
        if (review.operation_key, review.request_revision, review.selected_client_id, review.resolved_by_id) != (
                operation_key, expected_revision, client_id, user.pk):
            raise IdentityConflict("Identity resolution has already been decided.")
        return {"review_id": review.pk, "client_id": review.selected_client_id, "revision": review.revision}
    if review.revision != expected_revision or conversation.client_id is not None:
        raise IdentityConflict("Identity resolution version has changed.")
    # Linking the source confirms this decision only. It does not promote any
    # suggested/provider contact, overwrite manual fields, or upgrade Client status.
    conversation.client = client
    conversation.save(update_fields=["client", "updated_at"])
    observe(conversation, origin="resolution", kind="identity", value=str(client.pk), actor=user)
    review.state, review.selected_client_id, review.resolved_by = "resolved", client.pk, user
    review.operation_key, review.request_revision = operation_key, expected_revision
    review.resolved_at, review.revision = timezone.now(), review.revision + 1
    review.save()
    return {"review_id": review.pk, "client_id": client.pk, "revision": review.revision}
