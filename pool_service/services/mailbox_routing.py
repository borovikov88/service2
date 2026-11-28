"""Address history and staff-selected CRM links. Import never creates a lead or deal."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.db.models.functions import Lower, Trim
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientBusinessRole, ClientCompanyLink, ClientContact
from pool_service.client_queries import active_clients, readable_clients
from pool_service.crm_commercial_models import CrmLead, CrmLeadDealLink
from pool_service.mailbox_models import MailLetter
from pool_service.models import DataAuditLog, Organization
from .crm_card_privacy import exclude_private_only_cards
from .crm_commercial_access import deal_object_scope, deals_for_user
from .crm_communication_routing import active_deals
from .crm_lead_access import leads_for_user
from .crm_lead_commands import execute_lead_command
from .crm_lead_identity import active_leads
from .crm_role_runtime import resolve_crm_capabilities
from .mailbox_access import mailbox_role
from .mailbox_mime import digest, display_address, smtp_address


def address_clients(org, address):
    aliases = {display_address(address)}
    try:
        aliases.add(smtp_address(address))
    except ValidationError:
        pass
    extra = (
        ClientContact.objects.filter(client__organization=org, kind="email")
        .annotate(normalized=Lower(Trim("value")))
        .filter(Q(match_value__in=aliases) | Q(normalized__in=aliases))
        .values("client_id")
    )
    return (
        active_clients()
        .filter(organization=org)
        .annotate(normalized=Lower(Trim("email")))
        .filter(Q(normalized__in=aliases) | Q(pk__in=extra))
    )


def excluded(clients):
    return ClientBusinessRole.objects.filter(
        client__in=clients, active=True, role__in=("personal", "internal")
    ).exists()


def client_addresses(client):
    addresses = client.crm_contacts.filter(kind="email").values_list("value", flat=True)
    return set(display_address(a) for a in [client.email, *addresses] if a and a.strip())


def matched_parties(org, address):
    parties = list(address_clients(org, address)[:3])
    if len(parties) < 2:
        return parties
    if len(parties) == 2:
        person = next((c for c in parties if c.client_type == "private"), None)
        company = next((c for c in parties if c.client_type == "legal"), None)
        if person and company and ClientCompanyLink.objects.filter(person=person, company=company).exists():
            return [person, company]
    return None


def card_letters(org, subject, lead_ids):
    scope = Q(lead_id__in=lead_ids, lead__organization=org)
    if not isinstance(subject, CrmLead):
        scope |= Q(deal=subject, deal__organization=org)
    private = active_clients().filter(
        organization=org, business_roles__active=True, business_roles__role__in=("personal", "internal")
    )
    addresses = private.annotate(addr=Lower(Trim("email"))).values("addr")
    additional = (
        ClientContact.objects.filter(client__in=private, kind="email")
        .annotate(addr=Lower(Trim("value")))
        .values("addr")
    )
    blocked = (
        MailLetter.objects.filter(mailbox__organization=org)
        .filter(Q(peers__address__in=addresses) | Q(peers__address__in=additional))
        .values("pk")
    )
    return MailLetter.objects.filter(scope, mailbox__organization=org).filter(
        Q(link_kind="manual") | Q(commercial=True) & ~Q(pk__in=blocked)
    )


def contact_letters(org, client, *, letters=None):
    if client.organization_id != org.pk or excluded([client]):
        return MailLetter.objects.none()
    # A shared address in two unrelated cards should not expose either customer's mail in the other card.
    unique = [
        addr
        for addr in client_addresses(client)
        if client.pk in {item.pk for item in (matched_parties(client.organization_id, addr) or [])}
        and not excluded(address_clients(client.organization_id, addr))
    ]
    if letters is None:
        letters = MailLetter.objects.all()
    return letters.filter(mailbox__organization=org, commercial=True, peers__address__in=unique).distinct()


def _valid_leads(org):
    return exclude_private_only_cards(
        CrmLead.objects.filter(organization=org)
        .filter(Q(client__isnull=True) | Q(client__organization=org))
        .filter(Q(contact__isnull=True) | Q(contact__organization=org, contact__client_type="private")),
        org,
        kind="lead",
    )


def _valid_deals(org):
    return exclude_private_only_cards(
        deal_object_scope(org).filter(pipeline__organization=org, stage__pipeline_id=F("pipeline_id")),
        org,
        kind="deal",
    )


def automatic_link(letter, peer):
    """Only link an existing card. The caller owns the mailbox/organization write transaction."""
    if not peer or not letter.commercial or letter.lead_id or letter.deal_id:
        return
    if letter.historical and not letter.reference_ids:
        return
    org = letter.mailbox.organization
    clients = address_clients(org, peer)
    if excluded(clients):
        if letter.needs_attention:
            letter.needs_attention = False
            letter.save(update_fields=["needs_attention"])
        return
    matches = matched_parties(org, peer)
    if matches is None:
        return
    lead = deal = None
    link_kind = "thread"
    if letter.reference_ids:
        parents = letter.mailbox.letters.filter(
            message_key__in=[digest(ref) for ref in letter.reference_ids],
            commercial=True,
            peers__address=peer,
        ).exclude(lead=None, deal=None)
        # Only inherit a reference if it resolves unambiguously and concerns the same correspondent.
        for ref in reversed(letter.reference_ids):
            pair = list(parents.filter(message_id=ref).values_list("lead_id", "deal_id").distinct()[:2])
            if not pair:
                continue
            if len(pair) != 1:
                return
            lead_id, deal_id = pair[0]
            if lead_id:
                lead = _valid_leads(org).filter(pk=lead_id, is_archived=False).exclude(result="spam").first()
                if lead and lead.status == "closed":
                    linked = list(
                        _valid_deals(org).filter(
                            pk__in=CrmLeadDealLink.objects.filter(organization=org, lead=lead).values(
                                "deal_id"
                            ),
                            is_archived=False,
                        )[:2]
                    )
                    if len(linked) == 1:
                        lead, deal = None, linked[0]
            elif deal_id:
                deal = _valid_deals(org).filter(pk=deal_id, is_archived=False).first()
            break
    if not (lead or deal) and matches and not letter.historical:
        party = Q(contact__in=matches) | Q(client__in=matches)
        leads = list(active_leads(_valid_leads(org)).filter(party)[:2])
        deals = list(active_deals(_valid_deals(org)).filter(party)[:2])
        if len(leads) + len(deals) == 1:
            lead, deal = next(iter(leads), None), next(iter(deals), None)
            link_kind = "match"
    if lead or deal:
        letter.lead, letter.deal, letter.link_kind = lead, deal, link_kind
        letter.save(update_fields=["lead", "deal", "link_kind"])


def writable_cards(kind, user, org):
    grant = resolve_crm_capabilities(user.pk, org.pk)
    if kind == "lead":
        cards = leads_for_user(user, org).filter(is_archived=False).exclude(result="spam")
        return cards.filter(assignee=user) if org.crm_v2_write and grant.edit_own_leads else cards.none()
    cards = deals_for_user(user, org).filter(
        pipeline__organization=org, stage__pipeline_id=F("pipeline_id"), is_archived=False
    )
    if not org.crm_v2_write or not grant.edit_own_deals:
        return cards.none()
    return cards if mailbox_role(user, org) == "owner" else cards.filter(manager=user)


def visible_link(letter, user, org):
    if letter.lead_id:
        card = leads_for_user(user, org).filter(pk=letter.lead_id).first()
        kind, view = "lead", "crm_live_leads"
    elif letter.deal_id:
        card = (
            deals_for_user(user, org)
            .filter(pk=letter.deal_id, pipeline__organization=org, stage__pipeline_id=F("pipeline_id"))
            .first()
        )
        kind, view = "deal", "crm_live_workspace"
    else:
        return None
    if not card:
        return None
    suffix = "&pipeline=" + str(card.pipeline_id) if kind == "deal" else ""
    return {
        "title": card.title,
        "url": f"{reverse(view)}?org_id={org.pk}&{kind}={card.pk}{suffix}",
        "kind": "Лид" if kind == "lead" else "Сделка",
    }


@transaction.atomic
def bind_mail(user, org, letter_id, revision, kind, target=None, new_lead=None):
    org = Organization.objects.select_for_update().get(pk=org.pk)
    if not mailbox_role(user, org):
        raise PermissionDenied
    letter = MailLetter.objects.select_for_update().get(pk=letter_id, mailbox__organization=org)
    old = {"lead": letter.lead_id, "deal": letter.deal_id}
    for old_kind, pk in old.items():
        if pk and not writable_cards(old_kind, user, org).filter(pk=pk).exists():
            raise PermissionDenied
    if kind not in ("new_lead", "deal", "lead"):
        raise ValidationError("Выберите лид или сделку.")
    if kind != "new_lead":
        card = writable_cards(kind, user, org).select_for_update().filter(pk=target).first()
        if not card:
            raise PermissionDenied
        if old[kind] == card.pk and letter.link_kind == "manual":
            return letter
    if letter.revision != revision:
        raise ValidationError("Письмо уже обработал другой сотрудник. Обновите страницу.")
    if kind == "new_lead":
        if any(old.values()):
            raise ValidationError("Письмо уже связано с карточкой.")
        changes = dict(new_lead, acquisition_source="email", first_contact_channel="email")
        peers = list(letter.peers.values_list("address", flat=True)[:2])
        if len(peers) == 1:
            parties = matched_parties(org, peers[0])
            if parties and readable_clients(user).filter(
                pk__in=[party.pk for party in parties]
            ).count() == len(parties):
                changes["client_id"] = parties[-1].pk
                if parties[0].client_type == "private":
                    changes["contact_id"] = parties[0].pk
        result = execute_lead_command(
            actor=user,
            organization=org,
            command_type="lead.create",
            operation_key=f"mail-lead-{letter.pk}",
            changes=changes,
        )
        target, kind = result["lead_id"], "lead"
    letter.lead_id = target if kind == "lead" else None
    letter.deal_id = target if kind == "deal" else None
    letter.link_kind, letter.linked_by = "manual", user
    letter.needs_attention = False
    letter.processed_at = timezone.now()
    letter.revision += 1
    letter.save(
        update_fields=[
            "lead",
            "deal",
            "link_kind",
            "linked_by",
            "needs_attention",
            "processed_at",
            "revision",
        ]
    )
    DataAuditLog.objects.create(
        actor=user,
        organization=org,
        entity_type="MailLetter",
        entity_id=str(letter.pk),
        action="update",
        before=old,
        after={"lead": letter.lead_id, "deal": letter.deal_id},
        changed_fields=["lead", "deal"],
    )
    return letter
