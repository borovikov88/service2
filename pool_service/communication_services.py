from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from pool_service.communication_models import CommunicationAccess, Conversation, ConversationMessage
from pool_service.models import OrganizationAccess
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
def receive_message(*, connection, external_conversation_id, participant_name, body, external_message_id="", participant_phone=""):
    conversation, _ = Conversation.objects.select_for_update().get_or_create(
        connection=connection,
        external_id=external_conversation_id,
        defaults={
            "organization": connection.channel.organization,
            "participant_name": participant_name,
            "participant_phone": participant_phone,
        },
    )
    if external_message_id:
        existing = ConversationMessage.objects.filter(
            conversation=conversation, external_id=external_message_id
        ).first()
        if existing:
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
        if not external_message_id:
            raise
        return ConversationMessage.objects.get(
            conversation=conversation,
            external_id=external_message_id,
        ), False
    conversation.participant_name = participant_name or conversation.participant_name
    conversation.participant_phone = participant_phone or conversation.participant_phone
    conversation.last_message_at = message.created_at
    if conversation.status == Conversation.STATUS_DONE:
        conversation.status = Conversation.STATUS_NEW
    conversation.save(update_fields=["participant_name", "participant_phone", "last_message_at", "status", "updated_at"])
    notify_users(
        users_with_conversation_access(conversation.organization),
        title="Новое сообщение",
        message=f"{connection.channel.get_kind_display()} · {participant_name}: {body[:120]}",
        kind="communication",
        action_url=reverse("communications_conversations") + f"?conversation={conversation.uuid}",
        organization=conversation.organization,
        dedupe_key=f"communication:{message.pk}",
        send_push=True,
    )
    return message, True
