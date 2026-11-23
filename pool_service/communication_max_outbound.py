"""Send a manager's reply to an existing private MAX dialog."""

import json
import ssl
from functools import lru_cache
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from time import sleep
from uuid import uuid5

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from pool_service.communication_max_contact import CONTACT_REQUEST_ATTACHMENT, CONTACT_REQUEST_TEXT
from pool_service.communication_max_media import MaxMediaError, upload_attachment
from pool_service.communication_models import ChannelConnection, CommunicationChannel, ConversationMessage
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
    "max_upload_failed": "Не удалось загрузить вложение в MAX. Повторите отправку файла.",
    "max_attachment_processing": "MAX ещё обрабатывает файл. Повторите отправку позже.",
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
            status = response.status
            if status == 429:
                raise MaxApiError("max_rate_limited")
            if status != 200 and not 400 <= status < 500:
                raise MaxApiError("max_delivery_unknown")
            raw = response.read(65537)
        if len(raw) > 65536:
            raise MaxApiError("max_delivery_unknown")
        try:
            data = json.loads(raw)
        except (ValueError, UnicodeError):
            if 400 <= status < 500:
                raise MaxApiError("max_rejected") from None
            raise
        if isinstance(data, dict) and (status >= 400 or data.get("success") is False):
            if data.get("code") == "attachment.not.ready":
                raise MaxApiError("max_attachment_processing")
            raise MaxApiError("max_rejected")
        if status != 200:
            raise MaxApiError("max_rejected")
        return data
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


@sensitive_variables("token", "text", "result", "attachments", "payload")
def send_text(token, user_id, text, attachments=None):
    payload = {"text": text} if text else {}
    if attachments:
        payload["attachments"] = attachments
    for delay in (0.5, 1, None):
        try:
            result = _request("POST", f"/messages?user_id={user_id}", token, json.dumps(payload))
            break
        except MaxApiError as exc:
            if str(exc) != "max_attachment_processing" or delay is None or not attachments:
                raise
            sleep(delay)
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


def _automatic_request_id(conversation):
    return uuid5(conversation.uuid, "max-automatic-phone-request")


def queue_phone_request(conversation):
    """Queue exactly one automatic request under the inbound conversation lock."""
    connection = conversation.connection
    if not (
        (connection.settings or {}).get("max_auto_request_phone") is True
        and connection.max_bot_token_encrypted and reply_recipient(conversation)
    ):
        return
    history = conversation.messages
    if history.filter(max_details__shared_phone__isnull=False).exists() or history.filter(
        max_details__contact_request=True,
    ).exists():
        return
    message, created = history.get_or_create(
        max_reply_id=_automatic_request_id(conversation),
        defaults={
            "direction": ConversationMessage.DIRECTION_OUT,
            "body": CONTACT_REQUEST_TEXT,
            "sender_name": "Бот MAX",
            "delivery_status": ConversationMessage.DELIVERY_PENDING,
            "max_details": {"contact_request": True, "automatic": True},
        },
    )
    if created:
        transaction.on_commit(lambda: dispatch_reply(connection.pk, message.pk), robust=True)


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
    automatic = (
        not message.sent_by_id and message.max_reply_id == _automatic_request_id(conversation)
        and message.max_details.get("automatic") is True
        and message.max_details.get("contact_request") is True
        and message.body == CONTACT_REQUEST_TEXT
    )
    if automatic:
        if (
            (connection.settings or {}).get("max_auto_request_phone") is not True
            or conversation.messages.filter(max_details__shared_phone__isnull=False).exists()
        ):
            _fail(message_id, "max_reply_forbidden")
            return
    elif not message.sent_by or not conversation_capability(
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
        if message.max_details.get("contact_request"):
            attachments = [CONTACT_REQUEST_ATTACHMENT]
        else:
            attachment = message.attachments.order_by("pk").first()
            attachments = [upload_attachment(token, attachment)] if attachment else None
            if attachment:
                if not ChannelConnection.objects.filter(
                    pk=connection.pk, channel_id=channel.pk, channel__organization_id=conversation.organization_id,
                    channel__is_active=True, channel__kind=CommunicationChannel.KIND_MAX, is_active=True,
                    external_id=connection.external_id, max_bot_token_encrypted=connection.max_bot_token_encrypted,
                ).exists():
                    _fail(message_id, "max_account_unavailable")
                    return
                if not conversation_capability(message.sent_by, "can_reply_conversations", conversation.organization):
                    _fail(message_id, "max_reply_forbidden")
                    return
        provider_id = send_text(token, recipient_id, message.body, attachments)
    except MaxMediaError:
        _fail(message_id, "max_upload_failed")
        return
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
