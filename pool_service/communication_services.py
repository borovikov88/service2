from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_models import CommunicationAccess, Conversation, ConversationMessage
from pool_service.models import Organization, OrganizationAccess
from pool_service.services.notifications import notify_users


def users_with_conversation_access(organization):
    users = User.objects.filter(
        organizationaccess__organization=organization,
        organizationaccess__role__in=("owner", "admin", "manager", "service", "installer"),
        communication_accesses__organization=organization,
        communication_accesses__can_view_conversations=True,
        is_active=True,
    ).distinct()
    return users


def organization_access(user, organization=None):
    if not user.is_authenticated:
        return None
    queryset = OrganizationAccess.objects.select_related("organization").filter(user=user)
    if organization is not None:
        queryset = queryset.filter(organization=organization)
    return queryset.first()


def conversation_capability(user, capability, organization=None):
    if user.is_superuser:
        return True
    access = organization_access(user, organization)
    if not access or access.role == "accountant":
        return False
    if capability not in {field.name for field in CommunicationAccess._meta.fields}:
        return False
    return CommunicationAccess.objects.filter(
        organization=access.organization,
        user=user,
    ).values_list(capability, flat=True).first() is True


def optimize_message_image(attachment):
    """Create a bounded JPEG preview while retaining the untouched original."""
    if not attachment.content_type.startswith("image/"):
        return
    from io import BytesIO
    from PIL import Image, ImageOps

    attachment.original.open("rb")
    try:
        image = ImageOps.exif_transpose(Image.open(attachment.original))
        attachment.width, attachment.height = image.size
        image.thumbnail((1920, 1920))
        if image.mode not in ("RGB", "L"):
            background = Image.new("RGB", image.size, "white")
            if "A" in image.getbands():
                background.paste(image, mask=image.getchannel("A"))
            else:
                background.paste(image)
            image = background
        output = BytesIO()
        image.save(output, "JPEG", quality=82, optimize=True)
        attachment.optimized.save(f"{attachment.pk}.jpg", ContentFile(output.getvalue()), save=False)
        attachment.save(update_fields=["optimized", "width", "height"])
    finally:
        attachment.original.close()


@transaction.atomic
def receive_message(*, connection, external_conversation_id, participant_name, body, external_message_id=None, participant_phone="", participant_external_id="", provider_context=None):
    if connection.pk:
        from .communication_models import ChannelConnection
        avito_workplace = connection.pk == 4 and connection.channel.kind == "avito"
        authenticated = (connection.channel_id, connection.channel.organization_id,
            str(connection.external_id), connection.api_token_hash,
            connection.is_active, connection.channel.is_active, connection.channel.kind)
        locked = ChannelConnection.objects.select_for_update().select_related("channel__organization").get(pk=connection.pk)
        current = (locked.channel_id, locked.channel.organization_id, str(locked.external_id),
                   locked.api_token_hash, locked.is_active, locked.channel.is_active, locked.channel.kind)
        if current != authenticated or not locked.is_active or not locked.channel.is_active:
            raise ValueError("avito_connection_binding_changed" if avito_workplace else "communication_connection_binding_changed")
        connection = locked
    Organization.objects.select_for_update().get(pk=connection.channel.organization_id)
    external_message_id = external_message_id or None
    conversation, _ = Conversation.objects.select_for_update().get_or_create(
        connection=connection,
        external_id=external_conversation_id,
        defaults={
            "organization": connection.channel.organization,
            "participant_name": participant_name,
            "participant_phone": participant_phone,
            "participant_external_id": participant_external_id,
        },
    )
    if conversation.organization_id != connection.channel.organization_id:
        raise ValueError("avito_conversation_binding_changed" if connection.pk == 4 and connection.channel.kind == "avito" else "conversation_organization_mismatch")
    if participant_external_id and conversation.participant_external_id and str(participant_external_id) != conversation.participant_external_id:
        raise ValueError("conversation_participant_mismatch")
    if participant_external_id and not conversation.participant_external_id:
        conversation.participant_external_id = str(participant_external_id)[:255]
        conversation.save(update_fields=["participant_external_id"])
    from .communication_clients import capture_client
    if provider_context:
        from .communication_enrichment import apply_chat_context_locked
        apply_chat_context_locked(conversation, provider_context)
    if external_message_id is not None:
        existing = ConversationMessage.objects.filter(
            conversation=conversation, external_id=external_message_id
        ).first()
        if existing:
            capture_client(conversation)
            return existing, False
    try:
        with transaction.atomic():
            message = ConversationMessage.objects.create(
                conversation=conversation,
                external_id=external_message_id,
                direction=ConversationMessage.DIRECTION_IN,
                body=body,
                sender_name=participant_name,
                delivery_status=ConversationMessage.DELIVERY_RECEIVED,
            )
    except IntegrityError:
        if external_message_id is None:
            raise
        return ConversationMessage.objects.get(
            conversation=conversation,
            external_id=external_message_id,
        ), False
    if participant_name and (not conversation.participant_name or not str(participant_name).isdigit() and participant_name != "Клиент Авито"):
        conversation.participant_name = participant_name
    conversation.participant_phone = participant_phone or conversation.participant_phone
    conversation.last_message_at = message.created_at
    if conversation.status == Conversation.STATUS_DONE:
        conversation.status = Conversation.STATUS_NEW
    conversation.save(update_fields=["participant_name", "participant_phone", "last_message_at", "status", "updated_at"])
    capture_client(conversation, body=body, message_id=message.pk)
    if connection.channel.kind == "avito" and not provider_context:
        from .communication_enrichment import start_client_enrichment_worker
        transaction.on_commit(lambda: start_client_enrichment_worker([conversation.pk]))
    from .avito_deferred_messages import partition_users
    recipients = partition_users(connection, message, users_with_conversation_access(conversation.organization))
    notify_users(
        recipients,
        title="Новое сообщение",
        message=f"{connection.channel.get_kind_display()} · {participant_name}: {body[:120]}",
        kind="communication",
        action_url=reverse("communications_conversations") + f"?conversation={conversation.uuid}",
        organization=conversation.organization,
        dedupe_key=f"communication:{message.pk}",
        send_push=True,
    )
    return message, True
