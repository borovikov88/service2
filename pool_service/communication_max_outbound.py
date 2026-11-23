"""Send a manager's text reply to an existing private MAX dialog."""

import json
import ssl
from functools import lru_cache
from http.client import HTTPException, HTTPSConnection
from pathlib import Path

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from pool_service.communication_models import CommunicationChannel, ConversationMessage
from pool_service.communication_secrets import CommunicationSecretError, decrypt_secret
from pool_service.communication_services import conversation_capability


MAX_API_HOST = "platform-api2.max.ru"
MAX_CA = Path(__file__).with_name("provider_certificates") / "max-ministry-root.pem"
MAX_TEXT_LENGTH = 4000
MAX_REPLY_ERRORS = {
    "max_account_unavailable": "Подключение MAX отключено или токен не настроен.",
    "max_recipient_invalid": "Не удалось подтвердить получателя в MAX.",
    "max_reply_forbidden": "Недостаточно прав на отправку ответа.",
    "max_rejected": "MAX отклонил сообщение.",
    "max_rate_limited": "MAX ограничил частоту отправки. Повторите позже.",
    "max_delivery_unknown": "MAX не подтвердил отправку. Проверьте чат перед повтором.",
}


class MaxApiError(Exception):
    """Provider failures without response content, credentials or message text."""


@lru_cache(maxsize=1)
def max_api_ssl_context():
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=str(MAX_CA))
    return context


@sensitive_variables("token", "connection", "headers", "raw", "body", "response")
def _request(method, path, token, body=None):
    connection = HTTPSConnection(MAX_API_HOST, timeout=8, context=max_api_ssl_context())
    headers = {"Authorization": token, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=body, headers=headers)
        with connection.getresponse() as response:
            if response.status == 429:
                raise MaxApiError("max_rate_limited")
            if 400 <= response.status < 500:
                raise MaxApiError("max_rejected")
            if response.status != 200:
                raise MaxApiError("max_delivery_unknown")
            raw = response.read(65537)
        if len(raw) > 65536:
            raise MaxApiError("max_delivery_unknown")
        return json.loads(raw)
    except (OSError, HTTPException, ValueError):
        raise MaxApiError("max_delivery_unknown") from None
    finally:
        connection.close()


@sensitive_variables("token", "info")
def verify_bot_token(token, expected_bot_id):
    info = _request("GET", "/me", token)
    if (
        not isinstance(info, dict) or info.get("is_bot") is not True
        or type(info.get("user_id")) is not int
        or str(info["user_id"]) != expected_bot_id
    ):
        raise MaxApiError("max_bot_mismatch")


def reply_recipient(conversation):
    user_id = conversation.external_id
    if (
        user_id and user_id == conversation.participant_external_id
        and user_id != conversation.connection.external_id
        and user_id.isascii() and user_id.isdecimal() and len(user_id) <= 19
        and not user_id.startswith("0") and int(user_id) < 2**63
    ):
        return user_id
    return ""


@sensitive_variables("token", "text", "result")
def send_text(token, user_id, text):
    result = _request("POST", f"/messages?user_id={user_id}", token, json.dumps({"text": text}))
    message = result.get("message") if isinstance(result, dict) else None
    body = message.get("body") if isinstance(message, dict) else None
    message_id = body.get("mid") if isinstance(body, dict) else None
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 255 or "\x00" in message_id:
        raise MaxApiError("max_delivery_unknown")
    return message_id


def _fail(message_id, error):
    ConversationMessage.objects.filter(
        pk=message_id, delivery_status=ConversationMessage.DELIVERY_SENDING,
    ).update(delivery_status=ConversationMessage.DELIVERY_FAILED, delivery_error=error)


@sensitive_variables("token", "message")
def dispatch_reply(connection_id, message_id):
    """Claim one committed reply. Do not retry an uncertain provider send."""
    messages = ConversationMessage.objects.filter(
        pk=message_id,
        conversation__connection_id=connection_id,
        conversation__connection__channel__kind=CommunicationChannel.KIND_MAX,
        direction=ConversationMessage.DIRECTION_OUT,
        max_reply_id__isnull=False,
    )
    message = messages.select_related(
        "sent_by", "conversation__organization", "conversation__connection__channel",
    ).first()
    if not message or ConversationMessage.objects.filter(
        pk=message_id, delivery_status=ConversationMessage.DELIVERY_PENDING,
    ).update(
        delivery_status=ConversationMessage.DELIVERY_SENDING,
        delivery_attempts=F("delivery_attempts") + 1,
    ) != 1:
        return
    conversation = message.conversation
    connection = conversation.connection
    channel = connection.channel
    if (
        not channel.is_active or not connection.is_active or not connection.max_bot_token_encrypted
        or channel.organization_id != conversation.organization_id
    ):
        _fail(message_id, "max_account_unavailable")
        return
    if not message.sent_by or not conversation_capability(
        message.sent_by, "can_reply_conversations", conversation.organization,
    ):
        _fail(message_id, "max_reply_forbidden")
        return
    recipient_id = reply_recipient(conversation)
    if not recipient_id:
        _fail(message_id, "max_recipient_invalid")
        return

    try:
        token = decrypt_secret(connection.max_bot_token_encrypted)
        provider_id = send_text(token, recipient_id, message.body)
    except CommunicationSecretError:
        _fail(message_id, "max_account_unavailable")
        return
    except MaxApiError as exc:
        _fail(message_id, str(exc))
        return

    # A provider ID is useful for correlation when unique within this dialog.
    delivered = {
        "delivery_status": ConversationMessage.DELIVERY_DELIVERED,
        "delivered_at": timezone.now(),
        "delivery_error": "",
    }
    claimed = ConversationMessage.objects.filter(pk=message_id, delivery_status=ConversationMessage.DELIVERY_SENDING)
    try:
        with transaction.atomic():
            claimed.update(external_id=provider_id, **delivered)
    except IntegrityError:
        claimed.update(**delivered)
