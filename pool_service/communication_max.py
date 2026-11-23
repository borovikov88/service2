"""Inbound message parsing for the official MAX bot webhook."""

from datetime import datetime, timedelta, timezone

from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from pool_service.communication_max_contact import shared_phone
from pool_service.communication_max_media import MEDIA_NAMES, media_fingerprint, media_name
from pool_service.communication_models import MessageAttachment
from pool_service.communication_secrets import CommunicationSecretError, decrypt_secret
from pool_service.communication_services import receive_message


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _identifier(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("invalid_max_identifier")
    return str(value)


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("invalid_max_update")
    return value


@sensitive_variables("payload", "message", "body", "attachments")
def parse_message(payload, bot_id):
    if payload.get("update_type") != "message_created":
        return None

    message = _object(payload.get("message"))
    recipient = _object(message.get("recipient"))
    if recipient.get("chat_type") != "dialog":
        return None
    target = recipient.get("user_id")
    if target is not None and _identifier(target) != bot_id:
        return None

    sender = _object(message.get("sender"))
    if sender.get("is_bot") is True:
        return None
    sender_id = _identifier(sender.get("user_id"))
    if sender_id == bot_id:
        return None
    body = _object(message.get("body"))
    message_id = body.get("mid")
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > 255 or "\x00" in message_id:
        raise ValueError("invalid_max_message_id")

    text = body.get("text")
    if text is None:
        text = ""
    if not isinstance(text, str) or len(text) > 10000 or "\x00" in text:
        raise ValueError("invalid_max_message_text")
    text = text.strip()
    attachments = body.get("attachments")
    if attachments is not None and not isinstance(attachments, list):
        raise ValueError("invalid_max_attachments")
    if not text and not attachments:
        return None

    names = (sender.get("first_name"), sender.get("last_name"))
    name = " ".join(value.strip() for value in names if isinstance(value, str) and value.strip())
    millis = message.get("timestamp")
    if type(millis) is not int or not 946684800000 <= millis < 4133980800000:
        raise ValueError("invalid_max_timestamp")

    link = message.get("link")
    forwarded = isinstance(link, dict) and link.get("type") == "forward"
    return {
        "external_conversation_id": sender_id,
        "participant_external_id": sender_id,
        "participant_name": name.replace("\x00", "")[:255].strip() or "Клиент MAX",
        "body": text,
        "external_message_id": message_id,
        "occurred_at": EPOCH + timedelta(milliseconds=millis),
    }, (attachments or [])[:12], forwarded


@sensitive_variables("parsed", "attachments", "attachment", "attachment_data", "phone", "token")
@transaction.atomic
def ingest_update(connection, parsed):
    incoming, attachments, forwarded = parsed
    media = []
    phone = ""
    unknown = False
    token = ""
    if (
        not forwarded and connection.max_bot_token_encrypted
        and any(isinstance(item, dict) and item.get("type") == "contact" for item in attachments)
    ):
        try:
            token = decrypt_secret(connection.max_bot_token_encrypted)
        except CommunicationSecretError:
            pass
    for position, attachment in enumerate(attachments):
        if not isinstance(attachment, dict):
            unknown = True
            continue
        kind, attachment_data = attachment.get("type"), attachment.get("payload")
        if isinstance(kind, str) and kind in MEDIA_NAMES:
            filename = attachment_data.get("filename") if isinstance(attachment_data, dict) else None
            media.append({
                "max_media_position": position,
                "max_media_kind": kind,
                "max_media_fingerprint": media_fingerprint(kind, attachment_data),
                "original_name": media_name(filename, kind),
                "content_type": "application/octet-stream",
            })
        elif kind == "contact":
            verified = shared_phone(attachment_data, token, incoming["participant_external_id"])
            if verified:
                phone = verified
            else:
                unknown = True
        else:
            unknown = True
    if not incoming["body"]:
        if phone:
            incoming["body"] = "Клиент поделился телефоном в MAX"
        elif len(media) == 1 and not unknown:
            incoming["body"] = MEDIA_NAMES[media[0]["max_media_kind"]].split(" MAX")[0] + " в MAX"
        else:
            incoming["body"] = "Вложение в MAX"
    message, created = receive_message(connection=connection, **incoming)
    if not created:
        return
    if phone:
        message.max_details = {"shared_phone": phone}
        message.save(update_fields=["max_details"])
    if media:
        MessageAttachment.objects.bulk_create([
            MessageAttachment(message=message, **fields) for fields in media
        ])
