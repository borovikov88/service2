"""Contact identity for intake and presentation; never a permission grant.

Event provenance stays in CrmLeadSource. Thread keys are only routing hints.
All routing/uniqueness checks run under the organization's writer lock.
"""
from dataclasses import dataclass
import hashlib
import json

from django.core.exceptions import ValidationError
from django.db.models import F, Q, Value, Window
from django.db.models.functions import Coalesce, NullIf, RowNumber

from pool_service.client_crm_models import ClientCompanyLink, ClientContact
from pool_service.client_phone_matching import clients_by_phone, clients_by_phones
from pool_service.client_queries import active_clients, readable_clients
from pool_service.communication_models import ChannelConnection, ConversationMessage, PhoneCall
from pool_service.crm_commercial_models import CrmLead, CrmLeadSource
from pool_service.crm_inbox_models import CrmInboxEvent
from pool_service.phone_utils import normalize_phone


def active_leads(queryset):
    return queryset.filter(is_archived=False, result="").exclude(status="closed")


def is_active(lead):
    return not lead.is_archived and not lead.result and lead.status != "closed"


def context_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


@dataclass
class Identity:
    client: object = None
    contact: object = None
    phone: str = ""
    ambiguous: bool = False

    @property
    def key(self):
        if self.ambiguous:
            return None
        if self.contact:
            return ["contact", self.contact.pk]
        # A firm's shared FK does not identify the employee calling it.
        if self.phone:
            return ["phone", self.phone]
        return None


def _clients(organization, user=None):
    return (readable_clients(user) if user is not None else active_clients()).filter(organization=organization)


def _identity(organization, client=None, contact=None, phone="", *, user=None, infer_company=True, projection=None):
    if contact is None and client and client.client_type == "private":
        contact = client
    if infer_company and contact and (client is None or client.pk == contact.pk):
        companies = (projection["companies"].get(contact.pk, []) if projection is not None else
            list(_clients(organization, user).filter(client_type="legal",
                pk__in=ClientCompanyLink.objects.filter(person=contact,
                    person__organization=organization, company__organization=organization).values("company_id"))[:2]))
        client = companies[0] if len(companies) == 1 else contact
    return Identity(client=client, contact=contact, phone=normalize_phone(phone))


def source_identity(organization, source, *, user=None, projection=None):
    if isinstance(source, PhoneCall):
        client_id, phone = source.client_id, source.phone_number
    else:
        client_id = source.conversation.client_id
        phone = source.conversation.provider_phone or source.conversation.participant_phone
    client = (projection["clients"].get(client_id) if projection is not None else
        _clients(organization, user).filter(pk=client_id).first() if client_id else None)
    if client_id and client is None:
        return Identity(ambiguous=True)  # Never bypass an unavailable explicit FK.
    matches = []
    if not client or client.client_type == "legal":
        matches = (projection["phones"].get(normalize_phone(phone), []) if projection is not None else
            clients_by_phone(organization, phone, limit=2))
    if client:
        if client.client_type == "legal" and len(matches) > 1:
            return Identity(client=client, ambiguous=True)
        if client.client_type == "legal" and len(matches) == 1:
            person = (projection["clients"].get(matches[0].pk) if projection is not None else
                _clients(organization, user).filter(pk=matches[0].pk, client_type="private").first())
            linked = person and person.client_type == "private" and (
                client.pk in {company.pk for company in projection["companies"].get(person.pk, [])}
                if projection is not None else ClientCompanyLink.objects.filter(
                    person=person, company=client, person__organization=organization,
                    company__organization=organization).exists())
            if linked:
                return _identity(organization, client, person, phone, user=user, infer_company=False)
        return _identity(organization, client, phone=phone, user=user, projection=projection)
    if len(matches) > 1:
        return Identity(ambiguous=True)
    if matches:
        client = (projection["clients"].get(matches[0].pk) if projection is not None else
            _clients(organization, user).filter(pk=matches[0].pk).first())
        if not client:
            return Identity(ambiguous=True)
    return _identity(organization, client, phone=phone, user=user, projection=projection)


def _load_sources(organization, links):
    from .crm_lead_access import _without_private_calls
    call_rows = PhoneCall.objects.filter(pk__in=[link.inbox.source_id for link in links if link.inbox.source_type == "call"],
        organization=organization, direction__in=("in", "out"), source_kind="telephony", connection__organization=organization,
        connection__is_active=True, intake_received_at__isnull=False
    ).filter(Q(client__isnull=True) | Q(client__organization=organization)).filter(
        Q(employee_profile__isnull=True) | Q(employee_profile__organization=organization)
    ).select_related("connection").only("pk", "organization_id", "client_id", "phone_number",
        "employee_id", "peer_employee_id", "owner_user_snapshot", "direction", "result", "started_at",
        "connection_id", "connection__external_id", "intake_scope", "external_id")
    calls = call_rows.in_bulk()
    readable_call_ids = set(_without_private_calls(call_rows).values_list("pk", flat=True))
    messages = ConversationMessage.objects.select_related("conversation__connection__channel").filter(
        pk__in=[link.inbox.source_id for link in links if link.inbox.source_type == "message"],
        conversation__organization=organization, conversation__connection__channel__organization=organization,
        conversation__connection__is_active=True, conversation__connection__channel__is_active=True,
        intake_received_at__isnull=False).filter(
            Q(conversation__client__isnull=True) | Q(conversation__client__organization=organization)).only(
                "pk", "intake_scope", "external_id", "conversation_id", "conversation__organization_id",
                "conversation__client_id", "conversation__provider_phone", "conversation__participant_phone",
                "conversation__external_id", "conversation__connection_id", "conversation__connection__external_id",
                "conversation__connection__channel_id", "conversation__connection__channel__kind").in_bulk()
    from .call_privacy import private_call_ids
    hidden = private_call_ids(calls.values())
    # Keep protected sources only to validate immutable provenance during routing.
    # They never supply identity or presentation data. Independent work in a mixed
    # card remains eligible; malformed or unavailable sources still fail closed.
    private_calls = {pk: call for pk, call in calls.items() if pk in hidden}
    calls = {pk: call for pk, call in calls.items() if pk in readable_call_ids and pk not in hidden}
    channels = ChannelConnection.objects.select_related("channel").filter(
        pk__in={link.inbox.channel_id_snapshot for link in links}, channel__organization=organization,
        is_active=True, channel__is_active=True).only("pk", "external_id", "channel_id", "channel__kind").in_bulk()
    return {"calls": calls, "private_calls": private_calls, "messages": messages, "channels": channels}


def identity_projection(leads, user):
    """One request-scoped batch; never reuse these permission-filtered data on writes."""
    if not leads:
        return None
    organization = leads[0].organization
    if any(lead.organization_id != organization.pk for lead in leads):
        raise ValueError("mixed_identity_projection_scope")
    from .crm_lead_access import _readable_sources
    links = list(_identity_links(_readable_sources(user, organization).filter(lead__in=leads)))
    projection = _load_sources(organization, links)
    projection["links"] = {}
    for link in links:
        projection["links"].setdefault(link.lead_id, []).append(link)
    sources = list(projection["calls"].values()) + list(projection["messages"].values())
    phone_values, ids = [], {pk for lead in leads for pk in (lead.client_id, lead.contact_id) if pk}
    for source in sources:
        if isinstance(source, PhoneCall):
            ids.add(source.client_id); phone_values.append(source.phone_number)
        else:
            ids.add(source.conversation.client_id)
            phone_values.append(source.conversation.provider_phone or source.conversation.participant_phone)
    phones = clients_by_phones(organization, phone_values, limit=2)
    ids.update(client.pk for matches in phones.values() for client in matches)
    companies = list(ClientCompanyLink.objects.filter(person_id__in=ids,
        person__organization=organization, company__organization=organization).values_list("person_id", "company_id"))
    ids.update(company for _, company in companies)
    clients = {client.pk: client for client in _clients(organization, user).filter(pk__in=ids)}
    projection.update(clients=clients, phones=phones, companies={}, identities={})
    for person, company in companies:
        if company in clients and clients[company].client_type == "legal":
            projection["companies"].setdefault(person, []).append(clients[company])
    return projection


def _identity_links(queryset):
    # Repeated deliveries cannot change a source's live contact identity. Keep
    # all distinct evidence, but do not materialize every webhook revision or
    # communication body while rendering the board.
    return queryset.annotate(_identity_rank=Window(RowNumber(),
        partition_by=[F("lead_id"), F("inbox__source_key")], order_by=F("pk").asc())
    ).filter(_identity_rank=1).select_related("inbox").only("pk", "lead_id", "inbox_id",
        "inbox__organization_id", "inbox__source_key", "inbox__source_type", "inbox__source_id",
        "inbox__provider_kind", "inbox__connection_id_snapshot", "inbox__channel_id_snapshot",
        "inbox__account_id_snapshot", "inbox__namespace", "inbox__external_id").order_by("pk")


def lead_identity(lead, *, user=None, validate_sources=False, client_cache=None, projection=None):
    """Resolve current source bindings without writes on GET or guessing a person.

    Legacy explicit bindings and user selections remain authoritative. Incoming
    cards follow their current sources, so later contact enrichment is visible.
    """
    organization = lead.organization
    explicit = None
    if lead.identity_origin != "incoming" and (lead.client_id or lead.contact_id):
        available = client_cache if client_cache is not None else {
            c.pk: c for c in _clients(organization, user).filter(pk__in=[lead.client_id, lead.contact_id])}
        if any(pk and pk not in available for pk in (lead.client_id, lead.contact_id)):
            return Identity(ambiguous=True)
        explicit = _identity(organization, available.get(lead.client_id), available.get(lead.contact_id),
            user=user, infer_company=False)
    if lead.identity_origin == "manual":
        explicit = explicit or Identity()
    if explicit is not None and not validate_sources:
        return explicit
    if validate_sources:
        if CrmLeadSource.objects.filter(lead=lead).exclude(organization=organization,
                inbox__organization=organization, inbox__scope_verified=True).exists():
            return Identity(ambiguous=True)
        if lead.origin_lead_id and lead_identity(lead.origin_lead, validate_sources=True).ambiguous:
            return Identity(ambiguous=True)
    if projection is None:
        links = CrmLeadSource.objects.filter(organization=organization, lead=lead,
            inbox__organization=organization, inbox__scope_verified=True)
        if user is not None:
            from .crm_lead_access import _readable_sources
            links = _readable_sources(user, organization).filter(lead=lead)
        links = list(_identity_links(links))
        source_data = _load_sources(organization, links)
    else:
        if validate_sources:
            raise ValueError("projection_is_read_only")
        links = projection["links"].get(lead.pk, [])
        source_data = projection
    calls, messages, channels = (source_data[key] for key in ("calls", "messages", "channels"))
    identities = []
    has_private_source = False
    cache = projection["identities"] if projection is not None else {}
    seen = set()
    for link in links:
        event = link.inbox
        if event.source_key in seen:
            continue
        seen.add(event.source_key)
        if event.source_type == "call":
            source = calls.get(event.source_id) or source_data["private_calls"].get(event.source_id)
        else:
            source = messages.get(event.source_id)
        if not source:
            return Identity(ambiguous=True)
        snapshot = (event.organization_id, event.provider_kind, event.connection_id_snapshot,
            event.channel_id_snapshot, event.account_id_snapshot, event.namespace, event.external_id)
        if tuple((source.intake_scope or {}).get(name) for name in
                ("organization", "provider", "connection", "channel", "account", "namespace", "external")) != snapshot:
            return Identity(ambiguous=True)
        channel = channels.get(event.channel_id_snapshot)
        if not channel or channel.channel.kind != event.provider_kind or str(channel.external_id) != event.account_id_snapshot:
            return Identity(ambiguous=True)
        from .crm_inbox import source_scope
        if event.source_type == "call":
            if not source.connection_id or str(source.connection.external_id) != event.account_id_snapshot:
                return Identity(ambiguous=True)
            live = source_scope("call", source, channel, source.connection)
        else:
            live = source_scope("message", source, source.conversation.connection)
        if tuple(live.get(name) for name in
                ("organization", "provider", "connection", "channel", "account", "namespace", "external")) != snapshot:
            return Identity(ambiguous=True)
        if event.source_type == "call" and event.source_id in source_data["private_calls"]:
            has_private_source = True
            continue
        cache_key = ((source.client_id, normalize_phone(source.phone_number)) if event.source_type == "call" else
            (source.conversation.client_id, normalize_phone(source.conversation.provider_phone or source.conversation.participant_phone)))
        if cache_key not in cache:
            cache[cache_key] = source_identity(organization, source, user=user, projection=projection)
        identities.append(cache[cache_key])
    if has_private_source and not identities:
        return Identity(ambiguous=True)
    if explicit is not None:
        return explicit
    keys = {tuple(identity.key) for identity in identities if identity.key}
    if any(identity.ambiguous for identity in identities) or len(keys) > 1:
        return Identity(ambiguous=True)
    return next((identity for identity in identities if identity.key), Identity())


def matching_active_leads(organization, identity, *, exclude=None, using="default"):
    if identity.key is None:
        return []
    from .crm_lead_access import _with_phone_key
    phones = {identity.phone} - {""}
    person_id = identity.contact.pk if identity.contact else None
    if identity.contact:
        phones.add(normalize_phone(identity.contact.phone))
        phones.update(ClientContact.objects.using(using).filter(client=identity.contact,
            kind=ClientContact.KIND_PHONE).values_list("match_value", flat=True))
    phones.discard("")
    calls = PhoneCall.objects.using(using).filter(organization=organization, direction__in=("in", "out"))
    messages = ConversationMessage.objects.using(using).filter(conversation__organization=organization,
        direction="in").annotate(_identity_phone=Coalesce(NullIf("conversation__provider_phone", Value("")),
            "conversation__participant_phone"))
    call_match = Q(pk__in=_with_phone_key(calls).filter(_crm_phone_key__in=phones).values("pk"))
    message_match = Q(pk__in=_with_phone_key(messages, "_identity_phone").filter(_crm_phone_key__in=phones).values("pk"))
    direct = Q(pk__in=[])
    if person_id:
        call_match |= Q(client_id=person_id)
        message_match |= Q(conversation__client_id=person_id)
        direct = Q(contact_id=person_id) | Q(contact__isnull=True, client_id=person_id)
    events = CrmInboxEvent.objects.using(using).filter(organization=organization, scope_verified=True).filter(
        Q(source_type="call", source_id__in=calls.filter(call_match).values("pk")) |
        Q(source_type="message", source_id__in=messages.filter(message_match).values("pk")))
    inferred = (Q(identity_origin="incoming") | Q(identity_origin="legacy", client__isnull=True, contact__isnull=True)) & Q(sources__inbox__in=events)
    # Restrict by indexed FKs / scoped source queries before locking candidates,
    # without walking unrelated active cards and their history in Python.
    candidates = active_leads(CrmLead.objects.using(using).select_for_update().filter(
        direct | inferred, organization=organization)).exclude(pk=exclude).select_related("organization", "client", "contact").distinct()
    matches = []
    for lead in candidates:
        if lead_identity(lead, validate_sources=True).key == identity.key:
            matches.append(lead)
            if len(matches) == 2:
                break
    return matches


def ensure_single_active_lead(lead, *, using="default"):
    if is_active(lead) and matching_active_leads(lead.organization, lead_identity(lead), exclude=lead.pk, using=using):
        # Do not reveal the other card's ID, owner or title to this actor.
        raise ValidationError("Для этого контакта уже есть активный лид. Используйте существующее обращение.")


def ensure_merged_contact_episode(organization, client):
    """Check final merged identity under the shared organization writer lock."""
    if client.client_type == "private":
        identities = [Identity(client=client, contact=client)]
    else:
        phones = {normalize_phone(client.phone)}
        phones.update(normalize_phone(value) for value in ClientContact.objects.filter(
            client=client, kind=ClientContact.KIND_PHONE).values_list("value", flat=True))
        identities = [Identity(client=client, phone=phone) for phone in phones if phone]
    if any(len(matching_active_leads(organization, identity)) > 1 for identity in identities):
        # Keep separate people at one company distinct and reveal no other lead.
        raise ValidationError("Объединение создаст несколько активных лидов одного контакта. "
            "Сначала разберите обращения; история не изменена.")


def history_phone_key(organization, identity):
    """Only unbound calls may fall back to this unambiguous source phone."""
    if identity.ambiguous or not identity.phone:
        return ""
    matches = clients_by_phone(organization, identity.phone, limit=2)
    if len(matches) > 1 or matches and identity.contact and matches[0].pk != identity.contact.pk:
        return ""
    return identity.phone
