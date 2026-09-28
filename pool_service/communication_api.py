import json
from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from pool_service.communication_models import ChannelConnection, Conversation, ConversationMessage, MessageAttachment, WebsiteRequest
from pool_service.communication_services import receive_message
from pool_service.communication_avito import AvitoError, ingest_webhook


MAX_BODY_BYTES = 64 * 1024


def _error(message, status=400):
    return JsonResponse({"error": message}, status=status)


def _connection(request, public_id):
    remote_address = request.META.get("REMOTE_ADDR", "unknown")
    throttle_key = f"website-communications:{public_id}:{remote_address}"
    if cache.add(throttle_key, 1, timeout=60):
        request_count = 1
    else:
        try:
            request_count = cache.incr(throttle_key)
        except ValueError:
            cache.set(throttle_key, 1, timeout=60)
            request_count = 1
    if request_count > 300:
        return None
    connection = get_object_or_404(
        ChannelConnection.objects.select_related("channel__organization"),
        public_id=public_id,
        is_active=True,
        channel__is_active=True,
        channel__kind="website",
    )
    authorization = request.headers.get("Authorization", "")
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    if not connection.check_api_token(token):
        return None
    return connection


def _avito_connection(request, public_id, webhook_token):
    remote_address = request.META.get("REMOTE_ADDR", "unknown")
    throttle_key = f"avito-webhook:{public_id}:{remote_address}"
    if cache.get(throttle_key, 0) >= 300:
        return None
    try:
        cache.incr(throttle_key)
    except ValueError:
        cache.set(throttle_key, 1, timeout=60)
    connection = get_object_or_404(
        ChannelConnection.objects.select_related("channel__organization"),
        public_id=public_id,
        is_active=True,
        channel__is_active=True,
        channel__kind="avito",
    )
    return connection if connection.check_api_token(webhook_token) else None


def _payload(request):
    if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY_BYTES:
        raise ValueError("payload_too_large")
    raw_body = request.body
    if len(raw_body) > MAX_BODY_BYTES:
        raise ValueError("payload_too_large")
    try:
        data = json.loads(raw_body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError("invalid_json")
    if not isinstance(data, dict):
        raise ValueError("invalid_json")
    return data


def _text(data, name, limit, required=False):
    value = data.get(name, "")
    if not isinstance(value, str):
        raise ValueError(f"invalid_{name}")
    value = value.strip()
    if required and not value:
        raise ValueError(f"missing_{name}")
    return value[:limit]


def _identifier(data, name, limit):
    value = data.get(name, "")
    if not isinstance(value, str):
        raise ValueError(f"invalid_{name}")
    value = value.strip()
    if not value:
        raise ValueError(f"missing_{name}")
    if len(value) > limit:
        raise ValueError(f"invalid_{name}")
    return value


@csrf_exempt
@require_POST
def website_chat_message(request, public_id):
    connection = _connection(request, public_id)
    if not connection:
        return _error("unauthorized", 401)
    try:
        data = _payload(request)
        session_id = _identifier(data, "session_id", 250)
        message_id = _identifier(data, "message_id", 255)
        name = _text(data, "name", 255, required=True)
        phone = _text(data, "phone", 40)
        body = _text(data, "body", 10000, required=True)
    except ValueError as exc:
        return _error(str(exc))
    message, created = receive_message(
        connection=connection,
        external_conversation_id=f"chat:{session_id}",
        participant_name=name,
        participant_phone=phone,
        body=body,
        external_message_id=message_id,
    )
    return JsonResponse({"message_id": message.pk, "conversation_id": str(message.conversation.uuid), "created": created}, status=201 if created else 200)


@csrf_exempt
@require_POST
@transaction.atomic
def website_request_create(request, public_id):
    connection = _connection(request, public_id)
    if not connection:
        return _error("unauthorized", 401)
    try:
        data = _payload(request)
        request_id = _identifier(data, "request_id", 247)
        name = _text(data, "name", 255, required=True)
        phone = _text(data, "phone", 40, required=True)
        service = _text(data, "service", 255)
        delivery = _text(data, "delivery", 255)
        address = _text(data, "address", 500)
        comment = _text(data, "comment", 10000)
    except ValueError as exc:
        return _error(str(exc))
    message, created = receive_message(
        connection=connection,
        external_conversation_id=f"request:{request_id}",
        participant_name=name,
        participant_phone=phone,
        body=comment or f"Заявка: {service}",
        external_message_id=f"request:{request_id}",
    )
    website_request, _ = WebsiteRequest.objects.get_or_create(
        conversation=message.conversation,
        defaults={"service": service, "delivery": delivery, "address": address, "comment": comment, "submitted_at": timezone.now()},
    )
    return JsonResponse({"request_id": website_request.pk, "conversation_id": str(message.conversation.uuid), "created": created}, status=201 if created else 200)


@csrf_exempt
@require_http_methods(["GET"])
def website_outbox(request, public_id, session_id):
    connection = _connection(request, public_id)
    if not connection:
        return _error("unauthorized", 401)
    if len(session_id) > 250:
        return _error("invalid_session_id")
    conversation = get_object_or_404(Conversation, connection=connection, external_id=f"chat:{session_id}")
    try:
        after = max(0, int(request.GET.get("after", "0")))
    except ValueError:
        return _error("invalid_after")
    message_ids = list(
        conversation.messages.filter(
            direction=ConversationMessage.DIRECTION_OUT,
            delivery_status__in=(
                ConversationMessage.DELIVERY_PENDING,
                ConversationMessage.DELIVERY_SENDING,
            ),
            pk__gt=after,
        ).order_by("pk").values_list("pk", flat=True)[:100]
    )
    ConversationMessage.objects.filter(
        pk__in=message_ids,
        delivery_status=ConversationMessage.DELIVERY_PENDING,
    ).update(delivery_status=ConversationMessage.DELIVERY_SENDING, delivery_error="")
    messages = conversation.messages.filter(
        pk__in=message_ids,
        delivery_status=ConversationMessage.DELIVERY_SENDING,
    ).order_by("pk")
    result = []
    for item in messages.prefetch_related("attachments"):
        attachments = [{
            "id": attachment.pk,
            "name": attachment.original_name,
            "content_type": attachment.content_type,
            "size": attachment.original_size,
            "url": request.build_absolute_uri(reverse("website_chat_attachment", args=[public_id, session_id, attachment.pk])),
        } for attachment in item.attachments.all()]
        result.append({"id": item.pk, "body": item.body, "created_at": item.created_at.isoformat(), "attachments": attachments})
    return JsonResponse({"messages": result})


@csrf_exempt
@require_POST
def website_outbox_ack(request, public_id, session_id):
    connection = _connection(request, public_id)
    if not connection:
        return _error("unauthorized", 401)
    try:
        data = _payload(request)
        message_ids = data.get("message_ids", [])
        if len(session_id) > 250:
            raise ValueError("invalid_session_id")
        if not isinstance(message_ids, list) or len(message_ids) > 100 or not all(type(item) is int and item > 0 for item in message_ids):
            raise ValueError("invalid_message_ids")
    except ValueError as exc:
        return _error(str(exc))
    updated = ConversationMessage.objects.filter(
        conversation__connection=connection,
        conversation__external_id=f"chat:{session_id}",
        direction="out",
        delivery_status=ConversationMessage.DELIVERY_SENDING,
        pk__in=message_ids,
    ).update(delivery_status=ConversationMessage.DELIVERY_DELIVERED, delivered_at=timezone.now(), delivery_error="")
    return JsonResponse({"acknowledged": updated})


@csrf_exempt
@require_http_methods(["GET"])
def website_chat_attachment(request, public_id, session_id, attachment_id):
    connection = _connection(request, public_id)
    if not connection:
        return _error("unauthorized", 401)
    attachment = get_object_or_404(
        MessageAttachment,
        pk=attachment_id,
        message__direction="out",
        message__conversation__connection=connection,
        message__conversation__external_id=f"chat:{session_id}",
    )
    response = FileResponse(
        attachment.original.open("rb"),
        as_attachment=True,
        filename=attachment.original_name,
        content_type="application/octet-stream",
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@csrf_exempt
@require_POST
def avito_webhook(request, public_id, webhook_token):
    connection = _avito_connection(request, public_id, webhook_token)
    if not connection:
        return _error("unauthorized", 401)
    try:
        payload = _payload(request)
        message, created = ingest_webhook(connection, payload)
    except (ValueError, AvitoError) as exc:
        return _error(str(exc))
    return JsonResponse({
        "accepted": True,
        "created": created,
        "message_id": message.pk if message else None,
    }, status=201 if created else 200)
