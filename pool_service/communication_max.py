"""Inbound message parsing for the official MAX bot webhook."""

from datetime import datetime, timedelta, timezone


EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _identifier(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise ValueError("invalid_max_identifier")
    return str(value)


def _object(value):
    if not isinstance(value, dict):
        raise ValueError("invalid_max_update")
    return value


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
    if attachments:
        text = (text + "\n" if text else "") + "[Вложение в MAX]"
    if not text:
        return None

    names = (sender.get("first_name"), sender.get("last_name"))
    name = " ".join(value.strip() for value in names if isinstance(value, str) and value.strip())
    millis = message.get("timestamp")
    if type(millis) is not int or not 946684800000 <= millis < 4133980800000:
        raise ValueError("invalid_max_timestamp")

    return {
        "external_conversation_id": sender_id,
        "participant_external_id": sender_id,
        "participant_name": name.replace("\x00", "")[:255].strip() or "Клиент MAX",
        "body": text,
        "external_message_id": message_id,
        "occurred_at": EPOCH + timedelta(milliseconds=millis),
    }
