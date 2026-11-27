"""Project Avito callback numbers from their incoming messages; promotion is explicit."""
from collections import defaultdict

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, F, Max, OuterRef

from .client_crm_models import ClientContactExclusion
from .communication_contact_data import extract_contacts
from .communication_models import ChannelConnection, CommunicationAccess, CommunicationChannel, ConversationMessage
from .communication_services import conversation_capability, organization_access
from .contact_identity import suggested_role
from .contact_identity_models import ContactObservation
from .models import Client, Organization, OrganizationAccess
from .phone_utils import format_phone
from .services.contact_edit_access import contact_edit_roles
from .services.contact_people import manage_phone
from .services.crm_inbox import source_scope


def _messages(client):
    return ConversationMessage.objects.filter(
        conversation__client=client, conversation__organization_id=client.organization_id,
        conversation__connection__channel__organization_id=client.organization_id,
        conversation__connection__channel__kind=CommunicationChannel.KIND_AVITO,
        direction=ConversationMessage.DIRECTION_IN,
    ).select_related("conversation__connection__channel")


def _observations(client):
    return ContactObservation.objects.filter(
        origin="message", kind="phone", confirmation="suggested", source_kind=CommunicationChannel.KIND_AVITO,
        organization_id=client.organization_id, conversation__client=client,
        conversation__organization_id=client.organization_id,
        conversation__connection__channel__organization_id=client.organization_id,
        conversation__connection__channel__kind=F("source_kind"),
        connection_id_snapshot=F("conversation__connection_id"),
        account_id_snapshot=F("conversation__connection__external_id"),
        conversation_external_id=F("conversation__external_id"),
    )


def _phone_keys(message, observed_keys):
    # A recorded source receipt must still match. Older events without receipts
    # may use only their own account-scoped observations and surviving text.
    if message.intake_scope is not None or message.intake_received_at is not None:
        if not message.intake_received_at or message.intake_scope != source_scope(
                "message", message, message.conversation.connection):
            return []
        return extract_contacts(message.body)["phones"]
    return [key for key in extract_contacts(message.body)["phones"] if key in observed_keys]


def suggested_phones(actor, client, saved_phones):
    # Caller has checked can_view_conversations. The dialog also needs a
    # membership when the viewer is a Django superuser.
    if actor.is_superuser and not organization_access(actor, client.organization):
        return []
    messages = _messages(client)
    live_source = messages.filter(pk=OuterRef("source_message_id"), conversation_id=OuterRef("conversation_id"))
    observations = (_observations(client).filter(Exists(live_source)).values("value", "conversation_id")
                    .annotate(latest_message=Max("source_message_id")).order_by("-latest_message")[:30])
    observed = defaultdict(set)
    message_ids = set()
    for row in observations:
        message_ids.add(row["latest_message"])
        observed[row["conversation_id"], row["latest_message"]].add(row["value"])
    # Some saved inbound messages predate or missed extraction. Only inspect
    # a bounded set of phone-like texts; never write a contact on this read.
    recent = messages.filter(intake_received_at__isnull=False, body__regex=r"[0-9][0-9 ()+-]{6,}[0-9]")
    message_ids.update(recent.order_by("-pk").values_list("pk", flat=True)[:30])
    hidden = {row["key"] for row in saved_phones}
    if not message_ids:
        return []
    hidden.update(ClientContactExclusion.objects.filter(client=client, kind="phone").values_list("match_value", flat=True))
    result = []
    for message in messages.filter(pk__in=message_ids).order_by("-pk"):
        for key in _phone_keys(message, observed[message.conversation_id, message.pk]):
            if key in hidden:
                continue
            hidden.add(key)
            result.append({
                "message_id": message.pk, "key": key, "conversation": message.conversation.uuid,
                "role": suggested_role(message.body, key), "display": format_phone(key),
                "dial": key if key.startswith("+") else "+7" + key,
            })
    return result


@transaction.atomic
def confirm_phone(actor, client, message_id, key, revision):
    if not contact_edit_roles(actor, client) or not conversation_capability(actor, "can_view_conversations", client.organization):
        raise PermissionDenied
    stale = "Источник номера изменился. Откройте карточку ещё раз."
    messages = _messages(client).filter(pk=message_id)
    connection_id = messages.values_list("conversation__connection_id", flat=True).first()
    # Serialize source rebinds and incoming events in their normal order.
    connection = ChannelConnection.objects.select_for_update().select_related("channel").filter(
        pk=connection_id, channel__organization_id=client.organization_id, channel__kind=CommunicationChannel.KIND_AVITO,
    ).first()
    if not connection:
        raise ValidationError(stale)
    Organization.objects.select_for_update().get(pk=client.organization_id)
    message = messages.select_for_update().filter(conversation__connection_id=connection.pk).first()
    if not message:
        raise ValidationError(stale)
    client = Client.objects.select_for_update().filter(pk=client.pk, organization_id=client.organization_id).first()
    if not client:
        raise ValidationError(stale)
    if not contact_edit_roles(actor, client, lock=True):
        raise PermissionDenied
    # Directory editing and dialog access have distinct rules for multiple roles.
    # Hold the memberships and the communication grant while rechecking the latter.
    list(OrganizationAccess.objects.select_for_update().filter(user=actor, organization_id=client.organization_id)
         .order_by("pk").values_list("pk", flat=True))
    grant = CommunicationAccess.objects.select_for_update().filter(user=actor, organization_id=client.organization_id).first()
    if not grant or not conversation_capability(actor, "can_view_conversations", client.organization):
        raise PermissionDenied
    observed = _observations(client).filter(conversation=message.conversation, source_message_id=message.pk).values_list("value", flat=True)
    if key not in _phone_keys(message, observed) or suggested_role(message.body, key) != "unknown":
        raise ValidationError(stale)
    if ClientContactExclusion.objects.filter(client=client, kind="phone", match_value=key).exists():
        raise ValidationError("Этот номер был удалён из карточки.")
    manage_phone(actor, client.pk, action="add", value=key, label="Из сообщения Авито", primary="1", revision=revision)
