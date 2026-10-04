import json
from datetime import datetime, timezone as datetime_timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.http import FileResponse, JsonResponse
from django.core.cache import cache
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods, require_POST

from pool_service.communication_models import (
    ChannelConnection,
    Conversation,
    ConversationMessage,
    MessageAttachment,
    PhoneCall,
    TelephonyConnection,
    TelephonyEmployeeIdentity,
    WebsiteRequest,
)
from pool_service.communication_recordings import download_call_recording
from pool_service.communication_services import receive_message
from pool_service.services.employee_identity_sync import resolve_call_employee
from pool_service.models import Client, OrganizationAccess
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


def _mark_avito_webhook_event(connection, result, *, error=""):
    with transaction.atomic():
        locked = ChannelConnection.objects.select_for_update().get(pk=connection.pk)
        settings_data = dict(locked.settings or {})
        settings_data["avito_webhook_last_received_at"] = timezone.now().isoformat()
        settings_data["avito_webhook_last_result"] = result
        if error:
            settings_data["avito_webhook_last_error"] = error[:120]
        else:
            settings_data.pop("avito_webhook_last_error", None)
        locked.settings = settings_data
        locked.save(update_fields=["settings"])
        connection.settings = settings_data


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
        # Avito requires the registered webhook endpoint to answer HTTP 200.
        # Provider-contract errors are therefore acknowledged while a sanitized
        # diagnostic marker is retained for the owner.
        _mark_avito_webhook_event(connection, "error", error=str(exc))
        return JsonResponse({"accepted": True, "created": False, "error": str(exc)})
    result = "created" if created else ("duplicate" if message else "ignored")
    _mark_avito_webhook_event(connection, result)
    return JsonResponse({
        "accepted": True,
        "created": created,
        "message_id": message.pk if message else None,
    })


def _megafon_payload(request):
    content_type = (request.content_type or "").lower()
    if content_type.startswith("application/json"):
        return _payload(request)
    if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY_BYTES:
        raise ValueError("payload_too_large")
    data = request.POST.dict()
    if not data:
        raise ValueError("invalid_payload")
    return data


def _megafon_connection(request, public_id, data):
    remote_address = request.META.get("REMOTE_ADDR", "unknown")
    throttle_key = f"megafon-webhook:{public_id}:{remote_address}"
    if cache.add(throttle_key, 1, timeout=60):
        request_count = 1
    else:
        try:
            request_count = cache.incr(throttle_key)
        except ValueError:
            cache.set(throttle_key, 1, timeout=60)
            request_count = 1
    if request_count > 600:
        return None, None

    connection = get_object_or_404(
        ChannelConnection.objects.select_related("channel__organization"),
        public_id=public_id,
        is_active=True,
        channel__is_active=True,
        channel__kind="megafon",
    )
    crm_token = str(data.get("crm_token", "") or "").strip()
    if not connection.check_api_token(crm_token):
        return None, None
    telephony = get_object_or_404(
        TelephonyConnection,
        organization=connection.channel.organization,
        external_id=connection.external_id,
        is_active=True,
    )
    return connection, telephony


def _normalize_phone(value):
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) >= 10:
        return digits[-10:]
    return digits


def _megafon_contact(organization, phone):
    normalized = _normalize_phone(phone)
    if not normalized:
        return None
    for client in Client.objects.filter(organization=organization).only("id", "name", "phone"):
        if _normalize_phone(client.phone) == normalized:
            return client
    return None


def _megafon_employee(organization, provider_user, extension=""):
    candidates = {
        str(provider_user or "").strip().casefold(),
        str(extension or "").strip().casefold(),
    }
    candidates.discard("")
    if not candidates:
        return None
    user_ids = OrganizationAccess.objects.filter(
        organization=organization
    ).values_list("user_id", flat=True)
    for user in User.objects.filter(pk__in=user_ids):
        identifiers = {
            user.username.strip().casefold(),
            user.get_full_name().strip().casefold(),
            user.email.strip().casefold(),
        }
        identifiers.discard("")
        if identifiers & candidates:
            return user
    return None


def _megafon_identity_employee_for_replay(
    organization,
    telephony,
    extension="",
    provider_user="",
):
    identity = None
    if extension:
        identity = (
            TelephonyEmployeeIdentity.objects.filter(
                organization=organization,
                connection=telephony,
                extension=extension,
                is_active=True,
            )
            .select_related("employee__user")
            .first()
        )
        if (
            identity
            and identity.external_user
            and provider_user
            and identity.external_user != provider_user
        ):
            return None, None
    elif provider_user:
        candidates = list(
            TelephonyEmployeeIdentity.objects.filter(
                organization=organization,
                connection=telephony,
                external_user=provider_user,
                is_active=True,
            )
            .select_related("employee__user")
            .order_by("pk")[:2]
        )
        if len(candidates) == 1:
            identity = candidates[0]

    if (
        identity
        and identity.employee_id
        and not identity.requires_manual_confirmation
    ):
        return identity.employee, identity.employee.user
    return None, None


def _megafon_started_at(value):
    raw = str(value or "").strip()
    if not raw:
        return timezone.now()
    parsed = None
    for value_format in ("%Y-%m-%d %H:%M:%S", "%Y%m%dT%H%M%SZ"):
        try:
            parsed = datetime.strptime(raw, value_format)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("invalid_start")
    if raw.endswith("Z"):
        return parsed.replace(tzinfo=datetime_timezone.utc)
    try:
        provider_timezone = ZoneInfo(
            getattr(settings, "COMMUNICATION_TIME_ZONE", "UTC")
        )
    except ZoneInfoNotFoundError:
        provider_timezone = datetime_timezone.utc
    return timezone.make_aware(parsed, provider_timezone)


def _remember_megafon_recording_host(telephony, recording_ref):
    if not recording_ref:
        return
    try:
        parsed = urlsplit(recording_ref)
        hostname = parsed.hostname
        parsed.port
    except ValueError:
        return
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or not (hostname == "megapbx.ru" or hostname.endswith(".megapbx.ru"))
    ):
        return
    host = hostname.lower()
    hosts = telephony.recording_allowed_hosts
    if not isinstance(hosts, list):
        hosts = []
    normalized = [str(item).strip().lower() for item in hosts if isinstance(item, str) and item.strip()]
    if host in normalized:
        return
    normalized.append(host)
    TelephonyConnection.objects.filter(pk=telephony.pk).update(recording_allowed_hosts=normalized)
    telephony.recording_allowed_hosts = normalized


@csrf_exempt
@sensitive_post_parameters("crm_token")
@require_POST
def megafon_webhook(request, public_id):
    try:
        data = _megafon_payload(request)
    except ValueError as exc:
        return _error(str(exc))

    connection, telephony = _megafon_connection(request, public_id, data)
    if not connection:
        return _error("Invalid token", 401)

    cmd = str(data.get("cmd", "") or "").strip().lower()
    if cmd == "event":
        event_type = str(data.get("type", "") or "").strip().upper()
        phone = str(data.get("phone", "") or "").strip()
        extension = str(data.get("ext", "") or "").strip()
        provider_user = str(data.get("user", "") or "").strip()
        direction = str(data.get("direction", "") or "").strip().lower()
        call_id = str(data.get("callid", "") or "").strip()
        allowed_event_types = {
            "INCOMING",
            "ACCEPTED",
            "COMPLETED",
            "CANCELLED",
            "OUTGOING",
            "TRANSFERRED",
        }
        if event_type not in allowed_event_types:
            return _error("invalid_event_type")
        if not phone or len(phone) > 40:
            return _error("invalid_phone")
        if direction and direction not in {"in", "out"}:
            return _error("invalid_direction")
        if (
            len(extension) > 64
            or len(provider_user) > 255
            or len(call_id) > 255
        ):
            return _error("invalid_event")
        settings_data = dict(connection.settings or {})
        settings_data["megafon_last_event_at"] = timezone.now().isoformat()
        settings_data["megafon_last_event_type"] = event_type
        settings_data["megafon_last_event_phone"] = phone
        settings_data["megafon_last_event_ext"] = extension
        settings_data["megafon_last_event_user"] = provider_user
        settings_data["megafon_last_event_direction"] = direction
        settings_data["megafon_last_event_callid"] = call_id
        connection.settings = settings_data
        connection.save(update_fields=["settings"])
        resolve_call_employee(
            connection.channel.organization,
            telephony,
            extension,
            provider_user,
        )
        client = _megafon_contact(connection.channel.organization, phone)
        return JsonResponse({
            "accepted": True,
            "event": event_type,
            "contact_name": client.name if client else "",
        })

    if cmd == "contact":
        phone = str(data.get("phone", "") or "").strip()
        client = _megafon_contact(connection.channel.organization, phone)
        return JsonResponse({"contact_name": client.name} if client else {})

    if cmd != "history":
        return JsonResponse({"accepted": True, "ignored": True})

    call_id = str(data.get("callid", "") or "").strip()
    phone = str(data.get("phone", "") or "").strip()
    direction = str(data.get("type", "") or "").strip().lower()
    status = str(data.get("status", "") or "").strip()
    provider_user = str(data.get("user", "") or "").strip()
    extension = str(data.get("ext", "") or "").strip()
    recording_ref = str(data.get("link", "") or "").strip()

    if not call_id or len(call_id) > 255:
        return _error("invalid_callid")
    if not phone or len(phone) > 40:
        return _error("invalid_phone")
    if direction not in {PhoneCall.DIRECTION_IN, PhoneCall.DIRECTION_OUT}:
        return _error("invalid_type")
    if len(provider_user) > 255:
        return _error("invalid_user")
    if len(extension) > 64:
        return _error("invalid_ext")
    if len(recording_ref) > 500:
        return _error("invalid_link")

    try:
        duration_seconds = int(data.get("duration", 0))
    except (TypeError, ValueError):
        return _error("invalid_duration")
    if duration_seconds < 0 or duration_seconds > 7 * 24 * 60 * 60:
        return _error("invalid_duration")
    try:
        started_at = _megafon_started_at(data.get("start"))
    except ValueError as exc:
        return _error(str(exc))

    client = _megafon_contact(connection.channel.organization, phone)
    result = (
        PhoneCall.RESULT_ANSWERED
        if status.casefold() == "success"
        else PhoneCall.RESULT_MISSED
    )

    with transaction.atomic():
        existing_call = (
            PhoneCall.objects.select_for_update()
            .select_related("employee", "employee_profile")
            .filter(
                connection=telephony,
                external_id=call_id,
            )
            .first()
        )
        if existing_call:
            employee_profile, employee = _megafon_identity_employee_for_replay(
                connection.channel.organization,
                telephony,
                extension,
                provider_user,
            )
        else:
            employee_profile, employee = resolve_call_employee(
                connection.channel.organization,
                telephony,
                extension,
                provider_user,
            )
            unified_identity_exists = (
                not extension
                and bool(provider_user)
                and TelephonyEmployeeIdentity.objects.filter(
                    organization=connection.channel.organization,
                    connection=telephony,
                    external_user=provider_user,
                ).exists()
            )
            if employee is None and not extension and not unified_identity_exists:
                employee = _megafon_employee(
                    connection.channel.organization,
                    provider_user,
                    extension,
                )
        effective_employee = employee
        effective_employee_profile = employee_profile
        preserve_existing_ownership = existing_call and (
            existing_call.employee_profile_id
            or (
                existing_call.employee_id
                and (
                    existing_call.provider_extension
                    or existing_call.provider_user
                    or employee_profile is None
                )
            )
        )
        if preserve_existing_ownership:
            effective_employee = existing_call.employee
            effective_employee_profile = existing_call.employee_profile

        phone_call, created = PhoneCall.objects.update_or_create(
            connection=telephony,
            external_id=call_id,
            defaults={
                "organization": connection.channel.organization,
                "employee": effective_employee,
                "employee_profile": effective_employee_profile,
                "provider_user": provider_user,
                "provider_extension": extension,
                "contact_name": client.name if client else "",
                "phone_number": phone,
                "direction": direction,
                "started_at": started_at,
                "duration_seconds": duration_seconds,
                "result": result,
                "recording_ref": (
                    recording_ref
                    or (existing_call.recording_ref if existing_call else "")
                ),
            },
        )
        _remember_megafon_recording_host(telephony, recording_ref)
        settings_data = dict(connection.settings or {})
        settings_data["megafon_last_received_at"] = timezone.now().isoformat()
        settings_data["megafon_last_result"] = "created" if created else "updated"
        settings_data["megafon_last_history_user"] = provider_user
        settings_data["megafon_last_history_ext"] = extension
        settings_data["megafon_last_history_status"] = status
        connection.settings = settings_data
        connection.save(update_fields=["settings"])

        if recording_ref and not phone_call.recording_file:
            phone_call.recording_status = PhoneCall.RECORDING_PENDING
            phone_call.recording_error = ""
            phone_call.save(
                update_fields=["recording_status", "recording_error"]
            )

    recording_saved = False
    if (
        recording_ref
        and getattr(settings, "COMMUNICATION_RECORDING_DOWNLOAD_INLINE", True)
    ):
        recording_saved = download_call_recording(phone_call.pk)

    return JsonResponse({
        "accepted": True,
        "created": created,
        "call_id": phone_call.pk,
        "recording_saved": recording_saved,
    })
