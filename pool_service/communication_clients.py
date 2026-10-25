"""Tenant-scoped CRM capture for incoming conversations, without model calls."""
from django.db import transaction
from django.db.models import Q

from .client_crm_models import ClientContact, ClientCRMProfile
from .client_phone_matching import clients_by_phones
from .client_queries import active_clients
from .communication_contact_data import extract_contacts, phone_value, text
from .models import Client, Organization
from .phone_utils import normalize_phone


def _usable_name(value):
    value = text(value, 255)
    return value if value and not value.isdigit() and value not in {"Клиент", "Клиент Авито"} else ""


def capture_client(conversation, *, body="", message_id=None, create=True):
    """Caller holds organization then conversation locks in an atomic block."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("conversation_capture_requires_transaction")
    organization_id = conversation.organization_id
    if conversation.connection.channel.organization_id != organization_id:
        raise ValueError("conversation_organization_mismatch")
    found = extract_contacts(body)
    supplied_phone = normalize_phone(conversation.participant_phone)
    if supplied_phone and supplied_phone not in found["phones"]:
        found["phones"].insert(0, supplied_phone)
    previous = dict(conversation.contact_data) if isinstance(conversation.contact_data, dict) else {}
    client = None
    if conversation.client_id:
        client = Client.objects.select_for_update().get(pk=conversation.client_id)
        if client.organization_id != organization_id:
            raise ValueError("conversation_client_organization_mismatch")
    elif conversation.participant_external_id and conversation.connection.channel.kind == "avito":
        from .communication_models import Conversation
        ids = list(Conversation.objects.filter(
            organization_id=organization_id, connection__channel__organization_id=organization_id,
            connection__channel__kind="avito", participant_external_id=conversation.participant_external_id,
            client__organization_id=organization_id,
        ).exclude(client__isnull=True).values_list("client_id", flat=True).distinct()[:2])
        if len(ids) == 1:
            client = active_clients(Client.objects.select_for_update().filter(pk=ids[0])).first()
    if client is None and not conversation.client_id:
        ids = set()
        if found["phones"]:
            organization = Organization.objects.get(pk=organization_id)
            matches = clients_by_phones(organization, found["phones"], limit=2)
            ids.update(item.pk for values in matches.values() for item in values)
        if not ids and found["emails"]:
            ids.update(active_clients(Client.objects.filter(organization_id=organization_id)).filter(
                crm_contacts__kind="email", crm_contacts__match_value__in=found["emails"],
            ).values_list("pk", flat=True).distinct()[:2])
            email_filter = Q(pk__in=[])
            for email in found["emails"]:
                email_filter |= Q(email__iexact=email)
            ids.update(active_clients(Client.objects.filter(organization_id=organization_id)).filter(email_filter).values_list("pk", flat=True)[:2])
        if len(ids) == 1:
            client = Client.objects.select_for_update().get(pk=ids.pop())
    created = client is None
    if created and not create:
        return None
    if created:
        name = found.get("name") or _usable_name(conversation.participant_name) or f"Клиент · {conversation.connection.channel.get_kind_display()}"
        client = Client.objects.create(organization_id=organization_id, client_type="private", name=name)
        ClientCRMProfile.objects.create(client=client, source=ClientCRMProfile.SOURCE_COMMUNICATIONS,
            communication_data={"applied_name": name, "name_source": "message" if found.get("name") else "provider",
                "name_message_id": message_id if found.get("name") and message_id else 0})
    conversation.client = client
    profile = ClientCRMProfile.objects.select_for_update().filter(client=client).first()
    tracking = dict(profile.communication_data) if profile and isinstance(profile.communication_data, dict) else {}
    # A human edit wins over the last value written by this conversation.
    if found.get("name"):
        candidate_name, name_source = found["name"], "message"
        last_name_message_id = tracking.get("name_message_id")
        if not created and (not message_id or isinstance(last_name_message_id, int) and message_id < last_name_message_id):
            candidate_name = ""
    elif tracking.get("name_source") != "message":
        candidate_name, name_source = _usable_name(conversation.participant_name), "provider"
    else:
        candidate_name, name_source = "", tracking.get("name_source")
    changed = []
    owned_name = profile and profile.source == ClientCRMProfile.SOURCE_COMMUNICATIONS and client.name == tracking.get("applied_name")
    if candidate_name and (not client.name or owned_name):
        if client.name != candidate_name:
            client.name = candidate_name
            changed.append("name")
        if profile and profile.source == ClientCRMProfile.SOURCE_COMMUNICATIONS:
            profile.communication_data = {**tracking, "applied_name": candidate_name, "name_source": name_source}
            if name_source == "message" and message_id:
                profile.communication_data["name_message_id"] = message_id
            profile.save(update_fields=["communication_data", "updated_at"])
    for kind, values, field in (("phone", found["phones"], "phone"), ("email", found["emails"], "email")):
        saved = list(previous.get(field + "s", [])) if isinstance(previous.get(field + "s"), list) else []
        for key in values:
            value = phone_value(key) if kind == "phone" else key
            if key not in saved:
                saved.append(key)
            contact = ClientContact.objects.filter(client=client, kind=kind, match_value=key).first()
            source = "communications:" + conversation.connection.channel.kind
            if contact is None:
                contact, _ = ClientContact.objects.get_or_create(client=client, kind=kind, value=value, defaults={
                    "match_value": key, "label": conversation.connection.channel.get_kind_display(),
                    "sources": [source], "source_reference": f"conversation:{conversation.uuid}",
                    "is_primary": not getattr(client, field),
                })
                if not contact.match_value:
                    contact.match_value = key
                    contact.save(update_fields=["match_value", "updated_at"])
            elif source not in (contact.sources or []):
                contact.sources = list(contact.sources or []) + [source]
                contact.save(update_fields=["sources", "updated_at"])
            if not getattr(client, field) and len(value) <= Client._meta.get_field(field).max_length:
                setattr(client, field, value)
                changed.append(field)
        previous[field + "s"] = saved[:30]
    for field in ("name", "address", "company"):
        if found.get(field):
            previous[field] = found[field]
            if message_id:
                evidence = previous.get("evidence") if isinstance(previous.get("evidence"), dict) else {}
                previous["evidence"] = {**evidence, field: message_id}
    if changed:
        client.save(update_fields=list(set(changed)))
    if not conversation.participant_phone and client.phone:
        conversation.participant_phone = client.phone
    conversation.contact_data = previous
    conversation.save(update_fields=["client", "participant_phone", "contact_data", "updated_at"])
    return client
