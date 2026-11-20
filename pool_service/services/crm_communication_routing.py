"""Route verified inbox references to a deal or a single manual decision."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Q

from pool_service.communication_models import PhoneCall
from pool_service.crm_commercial_models import (
    CrmCommunicationRoute, CrmConversationDeal, CrmLeadDealLink, CrmLeadThread,
)
from pool_service.models import DataAuditLog, OrganizationAccess
from pool_service.staff_call_models import CallWorkContext
from .crm_commercial_access import deal_object_scope, deals_for_user
from .crm_card_privacy import exclude_private_only_cards
from .crm_lead_access import _source_access_annotations
from .crm_lead_identity import source_identity


def active_deals(queryset):
    return queryset.filter(is_archived=False, outcome="open", pipeline__is_active=True,
        pipeline__organization_id=F("organization_id"), stage__pipeline_id=F("pipeline_id"),
        stage__is_active=True).exclude(stage__code__in=("completed", "failed"))


def customer_deals(queryset, identity):
    if identity.ambiguous or not identity.client:
        return queryset.none()
    party = Q(client_id=identity.client.pk)
    if identity.contact:
        party |= Q(client_id=identity.contact.pk)
    return queryset.filter(party)


def route_to_deal(event, organization, identity, context_key, work_context=None):
    # Called under the writer lock shared by deal creation, transition and intake.
    old = CrmCommunicationRoute.objects.filter(organization=organization, source_key=event.source_key).first()
    if old:
        return "deal_linked" if old.deal_id else "needs_distribution"
    if work_context and work_context.lead_id:
        return None
    active = active_deals(deal_object_scope(organization))
    if identity.ambiguous:
        # Without a known deal context, the lead inbox handles uncertain callers.
        if not work_context:
            if not identity.client:
                return None
            company_deals = active.filter(client_id=identity.client.pk)
            if not exclude_private_only_cards(company_deals, organization, kind="deal").exists():
                return None
        CrmCommunicationRoute.objects.create(organization=organization, inbox=event, source_key=event.source_key)
        return "needs_distribution"
    candidates = customer_deals(active, identity)
    if identity.client:
        candidates = exclude_private_only_cards(candidates, organization, kind="deal")
    selected = None
    explicit = False
    if work_context:
        explicit = True
        selected = candidates.filter(pk=work_context.deal_id).first()
    if event.source_type == "message":
        dialog = CrmConversationDeal.objects.filter(organization=organization, context_key=context_key).first()
        if dialog:
            selected = candidates.filter(pk=dialog.deal_id).first()
        else:
            thread = CrmLeadThread.objects.filter(organization=organization, context_key=context_key).first()
            if thread:
                previous = list(CrmLeadDealLink.objects.filter(organization=organization, lead_id=thread.lead_id,
                    lead__organization=organization, lead__status="closed").order_by()
                    .values_list("deal_id", flat=True).distinct()[:2])
                if len(previous) == 1:
                    selected = candidates.filter(pk=previous[0]).first()
    if selected is None and not explicit:
        matches = list(candidates.order_by("pk")[:2])
        if len(matches) == 1 and (not identity.contact or matches[0].contact_id in {None, identity.contact.pk}):
            selected = matches[0]
        elif len(matches) == 0:
            return None
    CrmCommunicationRoute.objects.create(organization=organization, inbox=event, source_key=event.source_key, deal=selected)
    return "deal_linked" if selected else "needs_distribution"


def readable_routes(user, organization):
    routes = CrmCommunicationRoute.objects.filter(organization=organization, inbox__organization=organization,
        inbox__scope_verified=True).filter(Q(deal__isnull=True) | Q(deal__organization=organization))
    return _source_access_annotations(user, organization, source_links=routes).filter(_crm_channel=True).filter(
        Q(inbox__source_type="message", _crm_message=True) | Q(inbox__source_type="call", _crm_call=True))


def pending_routes(queryset, organization):
    explicit = CallWorkContext.objects.filter(organization=organization, call_id=OuterRef("inbox__source_id"))
    return queryset.filter(deal__isnull=True).exclude(Q(inbox__source_type="call") & Exists(explicit))


def route_source(route):
    from pool_service.communication_models import ConversationMessage
    proof = route.inbox
    if proof.source_type == "call":
        return PhoneCall.objects.filter(pk=proof.source_id, organization_id=route.organization_id).first()
    return ConversationMessage.objects.select_related("conversation").filter(
        pk=proof.source_id, conversation__organization_id=route.organization_id).first()


def editable_targets(user, organization):
    deals = active_deals(deals_for_user(user, organization))
    if OrganizationAccess.objects.filter(organization=organization, user=user, role="owner").exists():
        return deals
    return deals.filter(manager=user)


@transaction.atomic
def assign_route(*, user, organization, route_id, deal_id, revision, remember_dialog=False):
    from .crm_commercial_commands import _authorize
    from .crm_inbox import _locked_source
    from .crm_lead_intake import _context_key
    peek = CrmCommunicationRoute.objects.filter(organization=organization, pk=route_id).select_related("inbox").first()
    if not peek:
        raise PermissionDenied
    proof = peek.inbox
    org, source, error = _locked_source(proof.source_type, proof.source_id, proof.channel_id_snapshot, organization.pk)
    if error or not source or not org or (proof.source_type == 'call' and source.internal_contact_number_id):
        raise PermissionDenied
    user = _authorize("default", user.pk, org, "edit_own_deals")
    route = readable_routes(user, org).select_for_update().filter(pk=route_id).first()
    if not route:
        raise PermissionDenied
    targets = editable_targets(user, org)
    if route.deal_id and not deals_for_user(user, org).filter(pk=route.deal_id).filter(
            Q(manager=user) | Q(organization__accesses__user=user, organization__accesses__role="owner")).exists():
        raise PermissionDenied
    deal = targets.filter(pk=deal_id).first()
    if not deal:
        raise PermissionDenied
    identity = source_identity(org, source)
    if identity.client and not identity.ambiguous and not customer_deals(targets, identity).filter(pk=deal_id).exists():
        raise ValidationError("Выберите сделку этого клиента.")
    if remember_dialog and proof.source_type != "message":
        raise ValidationError("Связь с будущими сообщениями доступна только для диалога.")
    dialog_key = _context_key(proof) if remember_dialog else None
    dialog = CrmConversationDeal.objects.filter(organization=org, context_key=dialog_key).first() if dialog_key else None
    if route.revision != revision:
        if (route.deal_id == deal_id and route.revision == revision + 1
                and (not remember_dialog or dialog and dialog.deal_id == deal_id)):
            return route
        raise ValidationError("Связь уже изменена. Обновите страницу.")
    override = CallWorkContext.objects.filter(call_id=source.pk, organization=org).first() if proof.source_type == "call" else None
    if override and not (override.deal_id == deal_id and override.lead_id is None):
        from .call_access import set_work_context
        # Only the owner may change the authoritative per-call context.
        set_work_context(actor=user, call_id=source.pk, deal_id=deal_id, revision=override.revision)
    if remember_dialog:
        if dialog and dialog.deal_id != deal_id:
            if not deals_for_user(user, org).filter(pk=dialog.deal_id).filter(
                    Q(manager=user) | Q(organization__accesses__user=user, organization__accesses__role="owner")).exists():
                raise PermissionDenied
            previous_dialog_deal = dialog.deal_id
            dialog.deal = deal
            dialog.save(update_fields=["deal"])
            DataAuditLog.objects.create(organization=org, actor=user, entity_type="CrmConversationDeal",
                entity_id=str(dialog.pk), action="update", before={"deal_id": previous_dialog_deal},
                after={"deal_id": deal_id}, changed_fields=["deal"])
        elif not dialog:
            dialog = CrmConversationDeal.objects.create(organization=org, context_key=dialog_key, deal=deal)
            DataAuditLog.objects.create(organization=org, actor=user, entity_type="CrmConversationDeal",
                entity_id=str(dialog.pk), action="create", before={}, after={"deal_id": deal_id}, changed_fields=["deal"])
    if route.deal_id == deal_id:
        return route
    before = route.deal_id
    route.deal = deal
    route.revision += 1
    route.save(update_fields=["deal", "revision"])
    DataAuditLog.objects.create(organization=org, actor=user, entity_type="CrmCommunicationRoute",
        entity_id=str(route.pk), action="update", before={"deal_id": before},
        after={"deal_id": deal_id}, changed_fields=["deal"])
    return route
