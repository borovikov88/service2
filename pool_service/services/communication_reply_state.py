from django.db.models import OuterRef, Q, Subquery
from django.db.models.functions import Coalesce

from pool_service.communication_models import CommunicationChannel, ConversationMessage


def manager_reply_filter():
    # A storefront reply stays queued until the customer's browser picks it up.
    website_reply_queued = Q(
        conversation__connection__channel__kind=CommunicationChannel.KIND_WEBSITE,
        conversation__external_id__startswith="chat:",
        sent_by__isnull=False,
        delivery_status__in=(
            ConversationMessage.DELIVERY_PENDING,
            ConversationMessage.DELIVERY_SENDING,
        ),
    )
    return Q(direction=ConversationMessage.DIRECTION_OUT) & (
        Q(delivery_status=ConversationMessage.DELIVERY_DELIVERED) | website_reply_queued
    ) & (
        Q(max_details__contact_request__isnull=True) | Q(max_details__contact_request=False)
    )


def with_reply_state(conversations):
    latest_exchange = ConversationMessage.objects.filter(
        Q(direction=ConversationMessage.DIRECTION_IN) | manager_reply_filter(),
        conversation=OuterRef("pk"),
    ).order_by(Coalesce("intake_occurred_at", "created_at").desc(), "-pk")
    return conversations.annotate(last_exchange_direction=Subquery(latest_exchange.values("direction")[:1]))
