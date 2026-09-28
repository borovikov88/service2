from django.core.management.base import BaseCommand
from django.db import transaction

from pool_service.communication_avito import (
    AvitoAmbiguousDeliveryError,
    AvitoError,
    AvitoRetryableError,
    send_message,
)
from pool_service.communication_models import ChannelConnection, CommunicationChannel, ConversationMessage


MAX_TRANSIENT_ATTEMPTS = 3


def claim_message(message_id):
    """Claim only while channel and connection remain active under row locks."""
    with transaction.atomic():
        message = ConversationMessage.objects.select_for_update().select_related(
            "conversation__connection__channel",
        ).filter(
            pk=message_id,
            delivery_status=ConversationMessage.DELIVERY_PENDING,
        ).first()
        if message is None:
            return None
        connection_id = message.conversation.connection_id
        channel_id = message.conversation.connection.channel_id
        channel = CommunicationChannel.objects.select_for_update().get(pk=channel_id)
        connection = ChannelConnection.objects.select_for_update().get(pk=connection_id)
        if not channel.is_active or not connection.is_active:
            return None
        message.delivery_status = ConversationMessage.DELIVERY_SENDING
        message.delivery_attempts += 1
        message.save(update_fields=["delivery_status", "delivery_attempts"])
        return message


class Command(BaseCommand):
    help = "Send pending text messages for active Avito connections."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args, **options):
        limit = max(1, min(options["limit"], 500))
        message_ids = list(ConversationMessage.objects.filter(
            direction=ConversationMessage.DIRECTION_OUT,
            delivery_status=ConversationMessage.DELIVERY_PENDING,
            conversation__connection__channel__kind="avito",
            conversation__connection__channel__is_active=True,
            conversation__connection__is_active=True,
        ).order_by("pk").values_list("pk", flat=True)[:limit])
        delivered = retrying = failed = 0
        for message_id in message_ids:
            # Conditional update is the single-flight claim. A crashed/uncertain
            # delivery intentionally remains "sending" for manual reconciliation:
            # blind retries can duplicate a message accepted by the provider.
            message = claim_message(message_id)
            if message is None:
                continue
            try:
                send_message(message)
                delivered += 1
            except AvitoAmbiguousDeliveryError as exc:
                # Keep the claimed state: an operator must reconcile this with
                # Avito before deciding whether a retry is safe.
                message.delivery_error = str(exc)[:500]
                message.save(update_fields=["delivery_error"])
                failed += 1
            except AvitoRetryableError as exc:
                # Token acquisition and other pre-send transient failures are
                # known not to have delivered a customer message. Requeue them
                # only for a bounded number of attempts.
                message.delivery_error = str(exc)[:500]
                if message.delivery_attempts < MAX_TRANSIENT_ATTEMPTS:
                    message.delivery_status = ConversationMessage.DELIVERY_PENDING
                    retrying += 1
                else:
                    message.delivery_status = ConversationMessage.DELIVERY_FAILED
                    failed += 1
                message.save(update_fields=["delivery_status", "delivery_error"])
            except AvitoError as exc:
                message.delivery_status = ConversationMessage.DELIVERY_FAILED
                message.delivery_error = str(exc)[:500]
                message.save(update_fields=["delivery_status", "delivery_error"])
                failed += 1
        self.stdout.write(
            f"delivered={delivered} retrying={retrying} failed={failed}"
        )
