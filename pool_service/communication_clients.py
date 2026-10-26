"""Tenant-scoped CRM capture for incoming conversations, without model calls."""
from django.db import transaction

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
    from .contact_identity import observe, queue_review, suggested_role
    from .contact_identity_models import ContactIdentityReview, ContactObservation
    from .communication_models import Conversation
    if ContactObservation.objects.filter(conversation=conversation).exclude(
            organization_id=organization_id, connection_id_snapshot=conversation.connection_id,
            account_id_snapshot=str(conversation.connection.external_id), source_kind=conversation.connection.channel.kind,
            conversation_external_id=conversation.external_id).exists():
        raise ValueError("conversation_source_binding_changed")
    found = extract_contacts(body)
    # Every text contact is a suggestion tied to its actual incoming event.
    # Aggregated legacy bodies without a message ID cannot manufacture provenance.
    if message_id:
        for kind, values in (("phone", found["phones"]), ("email", found["emails"])):
            for value in values:
                observe(conversation, origin="message", kind=kind, value=value, message_id=message_id,
                    role=suggested_role(body, value) if kind == "phone" else "unknown")
    supplied_phone = normalize_phone(conversation.provider_phone)
    if supplied_phone:
        observe(conversation, origin="provider_phone", kind="phone", value=supplied_phone)
    if conversation.participant_external_id:
        observe(conversation, origin="provider_identity", kind="identity", value=conversation.participant_external_id)
    previous = dict(conversation.contact_data) if isinstance(conversation.contact_data, dict) else {}
    client = None
    if conversation.client_id:
        client = Client.objects.select_for_update().get(pk=conversation.client_id)
        if client.organization_id != organization_id:
            raise ValueError("conversation_client_organization_mismatch")
    else:
        # Pending clarification remains durable even if later matching changes;
        # enrichment/replay may not silently decide it or create a duplicate.
        review = ContactIdentityReview.objects.select_for_update().filter(conversation=conversation).first()
        if review:
            return None
        ids = set()
        reason = "provider_identity"
        if conversation.participant_external_id and conversation.connection.channel.kind == "avito":
            from django.db.models import Q
            sources = Conversation.objects.filter(organization_id=organization_id,
                connection_id=conversation.connection_id, participant_external_id=conversation.participant_external_id,
            ).filter(Q(contactobservation__isnull=True) | Q(contactobservation__origin="provider_identity",
                contactobservation__account_id_snapshot=str(conversation.connection.external_id)))
            ids.update(active_clients(Client.objects.filter(organization_id=organization_id,
                communication_conversations__pk__in=sources.values("pk"),
            )).values_list("pk", flat=True).distinct()[:51])
            # A pending source with this author blocks parallel provisional cards
            # in another chat; retain a local clarification for the local source.
            pending = ContactIdentityReview.objects.filter(
                organization_id=organization_id, state="pending",
                account_id_snapshot=str(conversation.connection.external_id),
                conversation__connection_id=conversation.connection_id,
                conversation__participant_external_id=conversation.participant_external_id,
            ).first()
            if pending:
                queue_review(conversation, pending.candidate_ids, pending.reason,
                    candidates_truncated=pending.candidates_truncated)
                return None
        if not ids and supplied_phone:
            reason = "provider_phone"
            organization = Organization.objects.get(pk=organization_id)
            matches = clients_by_phones(organization, [supplied_phone], limit=51)
            ids.update(item.pk for values in matches.values() for item in values)
        if len(ids) > 1:
            queue_review(conversation, ids, reason)
            return None
        if len(ids) == 1:
            client = Client.objects.select_for_update().get(pk=ids.pop())
    created = client is None
    if created and not create:
        return None
    if created:
        name = found.get("name") or _usable_name(conversation.participant_name) or f"Клиент · {conversation.connection.channel.get_kind_display()}"
        client = Client.objects.create(organization_id=organization_id, client_type="private", name=name)
        ClientCRMProfile.objects.create(client=client, source=ClientCRMProfile.SOURCE_COMMUNICATIONS, is_preliminary=True,
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
    # Only a real provider phone may enter the existing identity contacts.
    # Provider-supplied is still not a human confirmation of ownership.
    if supplied_phone:
        value = phone_value(supplied_phone)
        contact = ClientContact.objects.filter(client=client, kind="phone", match_value=supplied_phone).first()
        if contact is None:
            ClientContact.objects.get_or_create(client=client, kind="phone", value=value, defaults={
                "match_value": supplied_phone, "label": conversation.connection.channel.get_kind_display(),
                "sources": ["communications:" + conversation.connection.channel.kind],
                "source_reference": f"conversation:{conversation.uuid}", "is_primary": created and not client.phone,
            })
        if created and not client.phone and len(value) <= Client._meta.get_field("phone").max_length:
            client.phone = value
            changed.append("phone")
    for field in ("name", "address", "company"):
        if found.get(field):
            previous[field] = found[field]
            if message_id:
                evidence = previous.get("evidence") if isinstance(previous.get("evidence"), dict) else {}
                previous["evidence"] = {**evidence, field: message_id}
    if changed:
        client.save(update_fields=list(set(changed)))
    conversation.contact_data = previous
    conversation.save(update_fields=["client", "contact_data", "updated_at"])
    return client
