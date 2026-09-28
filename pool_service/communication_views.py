from datetime import date
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.files.images import get_image_dimensions
from django.db import IntegrityError, transaction
from django.db.models import OuterRef, Q, Subquery
from django.http import FileResponse, Http404, HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from django.views.decorators.csrf import ensure_csrf_cookie

from pool_service.communication_forms import (
    AvitoConnectionForm,
    MegafonConnectionForm,
    WebsiteConnectionForm,
)
from pool_service.communication_management import (
    configure_avito_credentials,
    get_or_create_provider_channel,
    rotate_connection_token,
)
from pool_service.communication_models import (
    AvitoCredential, ChannelConnection, CommunicationChannel, Conversation,
    ConversationAssignment, ConversationMessage, ConversationReadState,
    MessageAttachment, PhoneCall, TelephonyConnection,
)
from pool_service.communication_services import conversation_capability, optimize_message_image, organization_access
from pool_service.models import Notification, OrganizationAccess


def _context(request, capability):
    access = organization_access(request.user)
    if not access or not conversation_capability(request.user, capability, access.organization):
        raise PermissionDenied
    return access.organization


@login_required
def conversations(request):
    organization = _context(request, "can_view_conversations")
    latest_message = ConversationMessage.objects.filter(conversation=OuterRef("pk")).order_by("-created_at", "-pk")
    items = list(
        Conversation.objects.filter(organization=organization)
        .select_related("connection__channel", "assignee", "website_request")
        .annotate(last_message_preview=Subquery(latest_message.values("body")[:1]))
    )
    states = {state.conversation_id: state.last_read_at for state in ConversationReadState.objects.filter(user=request.user, conversation__organization=organization)}
    for item in items:
        incoming = item.messages.filter(direction=ConversationMessage.DIRECTION_IN)
        last_read = states.get(item.pk)
        item.unread_count = incoming.filter(created_at__gt=last_read).count() if last_read else incoming.count()
    selected = None
    selected_id = request.GET.get("conversation")
    if selected_id:
        selected = next((item for item in items if str(item.uuid) == selected_id), None)
        if selected is None:
            raise Http404
    elif items:
        selected = items[0]
    if selected:
        state, _ = ConversationReadState.objects.get_or_create(conversation=selected, user=request.user)
        state.last_read_at = timezone.now()
        state.save(update_fields=["last_read_at"])
        Notification.objects.filter(
            user=request.user,
            organization=selected.organization,
            kind="communication",
            action_url__contains=str(selected.uuid),
            is_resolved=False,
        ).update(is_read=True, is_resolved=True, resolved_at=timezone.now())
    return render(request, "pool_service/communications/conversations.html", {
        "active_tab": "communications", "conversations": items, "selected": selected,
        "can_reply": conversation_capability(request.user, "can_reply_conversations", organization),
        "can_take": conversation_capability(request.user, "can_take_conversation", organization),
        "can_assign": conversation_capability(request.user, "can_assign_conversation", organization),
        "staff": OrganizationAccess.objects.filter(organization=organization).exclude(role="accountant").select_related("user"),
    })


@login_required
@require_POST
@transaction.atomic
def conversation_reply(request, conversation_uuid):
    organization = _context(request, "can_reply_conversations")
    conversation = get_object_or_404(
        Conversation.objects.select_related("connection__channel"),
        uuid=conversation_uuid,
        organization=organization,
    )
    channel = get_object_or_404(
        CommunicationChannel.objects.select_for_update(),
        pk=conversation.connection.channel_id,
        organization=organization,
    )
    connection = get_object_or_404(
        ChannelConnection.objects.select_for_update(),
        pk=conversation.connection_id,
        channel=channel,
    )
    if not connection.is_active or not channel.is_active:
        messages.error(request, "Канал или подключение отключено. Отправка ответа недоступна.")
        return redirect(f"/communications/?conversation={conversation.uuid}")
    body = request.POST.get("body", "").strip()
    files = request.FILES.getlist("attachments")
    if len(body) > 10000:
        messages.error(request, "Сообщение не должно превышать 10 000 символов.")
        return redirect(f"/communications/?conversation={conversation.uuid}")
    if files and conversation.connection.channel.kind == CommunicationChannel.KIND_AVITO:
        messages.error(request, "Отправка фото и файлов в Авито пока не подключена.")
        return redirect(f"/communications/?conversation={conversation.uuid}")
    if len(files) > 10 or sum(upload.size for upload in files) > 25 * 1024 * 1024:
        messages.error(request, "Можно приложить до 10 файлов общим размером не более 25 МБ.")
        return redirect(f"/communications/?conversation={conversation.uuid}")
    if any(upload.size > 10 * 1024 * 1024 for upload in files):
        messages.error(request, "Размер одного файла не должен превышать 10 МБ.")
        return redirect(f"/communications/?conversation={conversation.uuid}")
    if not body and not files:
        messages.error(request, "Введите сообщение или добавьте файл.")
        return redirect("communications_conversations")
    for uploaded in files:
        content_type = uploaded.content_type or "application/octet-stream"
        if content_type.startswith("image/"):
            try:
                dimensions = get_image_dimensions(uploaded)
                if not dimensions or not all(dimensions):
                    raise ValueError("invalid dimensions")
                width, height = dimensions
                if width > 12000 or height > 12000 or width * height > 25_000_000:
                    messages.error(request, f"Изображение {uploaded.name} имеет слишком большое разрешение.")
                    return redirect(f"/communications/?conversation={conversation.uuid}")
                uploaded.seek(0)
            except Exception:
                messages.error(request, f"Файл {uploaded.name} не является корректным изображением.")
                return redirect(f"/communications/?conversation={conversation.uuid}")
    message = ConversationMessage.objects.create(
        conversation=conversation,
        direction="out",
        body=body,
        sender_name=request.user.get_full_name() or request.user.username,
        sent_by=request.user,
        delivery_status=ConversationMessage.DELIVERY_PENDING,
    )
    for uploaded in files:
        content_type = uploaded.content_type or "application/octet-stream"
        attachment = MessageAttachment.objects.create(message=message, original=uploaded, original_name=uploaded.name[:255], content_type=content_type, original_size=uploaded.size)
        try:
            optimize_message_image(attachment)
        except Exception:
            # The original remains available even when a safe preview cannot be produced.
            pass
    conversation.last_message_at = message.created_at
    conversation.save(update_fields=["last_message_at", "updated_at"])
    # Channel adapters send queued outbound messages; the persisted message is the source of truth.
    return redirect(f"/communications/?conversation={conversation.uuid}")


@login_required
@require_POST
@transaction.atomic
def conversation_take(request, conversation_uuid):
    organization = _context(request, "can_take_conversation")
    conversation = get_object_or_404(Conversation.objects.select_for_update(), uuid=conversation_uuid, organization=organization)
    if conversation.assignee_id and conversation.assignee_id != request.user.id:
        raise PermissionDenied
    conversation.assignee = request.user
    conversation.status = Conversation.STATUS_ACTIVE
    conversation.save(update_fields=["assignee", "status", "updated_at"])
    ConversationAssignment.objects.create(conversation=conversation, assignee=request.user, changed_by=request.user)
    Notification.objects.filter(
        organization=conversation.organization,
        kind="communication",
        action_url__contains=str(conversation.uuid),
        is_resolved=False,
    ).exclude(user=request.user).update(is_resolved=True, resolved_at=timezone.now())
    return redirect(f"/communications/?conversation={conversation.uuid}")


@login_required
@require_POST
@transaction.atomic
def conversation_update(request, conversation_uuid):
    organization = _context(request, "can_view_conversations")
    conversation = get_object_or_404(Conversation.objects.select_for_update(), uuid=conversation_uuid, organization=organization)
    status = request.POST.get("status")
    assignee_id = request.POST.get("assignee")
    if assignee_id is not None:
        if not conversation_capability(request.user, "can_assign_conversation", organization):
            raise PermissionDenied
        assignee = None
        if assignee_id:
            assignee = get_object_or_404(User, pk=assignee_id, organizationaccess__organization=organization)
        conversation.assignee = assignee
        conversation.save(update_fields=["assignee", "updated_at"])
        ConversationAssignment.objects.create(conversation=conversation, assignee=assignee, changed_by=request.user)
    if status in dict(Conversation.STATUS_CHOICES):
        if not conversation_capability(request.user, "can_take_conversation", organization):
            raise PermissionDenied
        if conversation.assignee_id not in (None, request.user.id) and not conversation_capability(request.user, "can_assign_conversation", organization):
            raise PermissionDenied
        conversation.status = status
        conversation.save(update_fields=["status", "updated_at"])
    return redirect(f"/communications/?conversation={conversation.uuid}")


@login_required
def attachment_download(request, attachment_id):
    organization = _context(request, "can_view_conversations")
    attachment = get_object_or_404(MessageAttachment, pk=attachment_id, message__conversation__organization=organization)
    response = FileResponse(
        attachment.original.open("rb"),
        as_attachment=True,
        filename=attachment.original_name,
        content_type="application/octet-stream",
    )
    response["X-Content-Type-Options"] = "nosniff"
    return response


@login_required
def calls(request):
    access = organization_access(request.user)
    if not access or not (
        conversation_capability(request.user, "can_view_own_calls", access.organization)
        or conversation_capability(request.user, "can_view_all_calls", access.organization)
    ):
        raise PermissionDenied
    organization = access.organization
    can_view_all = conversation_capability(request.user, "can_view_all_calls", organization)
    queryset = PhoneCall.objects.filter(organization=organization).select_related("employee")
    if not can_view_all:
        queryset = queryset.filter(employee=request.user)
    try:
        date_from = date.fromisoformat(request.GET["date_from"]) if request.GET.get("date_from") else None
        date_to = date.fromisoformat(request.GET["date_to"]) if request.GET.get("date_to") else None
    except ValueError:
        return HttpResponseBadRequest("Некорректный период.")
    if date_from and date_to and date_from > date_to:
        return HttpResponseBadRequest("Начало периода не может быть позже окончания.")
    if date_from: queryset = queryset.filter(started_at__date__gte=date_from)
    if date_to: queryset = queryset.filter(started_at__date__lte=date_to)
    employee_id = request.GET.get("employee")
    if employee_id and can_view_all:
        if not employee_id.isdigit() or not OrganizationAccess.objects.filter(organization=organization, user_id=employee_id).exists():
            return HttpResponseBadRequest("Некорректный сотрудник.")
        queryset = queryset.filter(employee_id=employee_id)
    if request.GET.get("direction") in ("in", "out"): queryset = queryset.filter(direction=request.GET["direction"])
    if request.GET.get("missed"): queryset = queryset.filter(result=PhoneCall.RESULT_MISSED)
    if request.GET.get("q"): queryset = queryset.filter(Q(phone_number__icontains=request.GET["q"]) | Q(contact_name__icontains=request.GET["q"]))
    employees = OrganizationAccess.objects.filter(organization=organization).select_related("user")
    return render(request, "pool_service/communications/calls.html", {
        "active_tab": "communications",
        "calls": queryset[:500],
        "employees": employees,
        "can_listen": conversation_capability(request.user, "can_listen_calls", organization),
        "can_view_all": can_view_all,
    })


@login_required
def call_recording(request, call_id):
    organization = _context(request, "can_listen_calls")
    call = get_object_or_404(PhoneCall, pk=call_id, organization=organization)
    if not conversation_capability(request.user, "can_view_all_calls", organization) and call.employee_id != request.user.id:
        raise PermissionDenied
    if not call.recording_ref or any(ord(character) <= 32 or ord(character) == 127 for character in call.recording_ref):
        raise Http404
    try:
        recording_url = urlsplit(call.recording_ref)
        # Accessing these properties performs bracket and port validation.
        hostname = recording_url.hostname
        recording_url.port
    except ValueError as exc:
        raise Http404 from exc
    configured_hosts = call.connection.recording_allowed_hosts
    if not isinstance(configured_hosts, list):
        raise Http404
    allowed_hosts = {
        str(item).strip().lower()
        for item in configured_hosts
        if isinstance(item, str) and item.strip()
    }
    if (
        recording_url.scheme != "https"
        or not hostname
        or hostname.lower() not in allowed_hosts
        or recording_url.port not in (None, 443)
        or recording_url.username
        or recording_url.password
    ):
        raise Http404
    return HttpResponseRedirect(call.recording_ref)


def _communication_connection_exists(*, organization, kind, external_id, exclude_id=None):
    queryset = ChannelConnection.objects.filter(
        channel__organization=organization,
        channel__kind=kind,
        external_id=external_id,
    )
    if exclude_id is not None:
        queryset = queryset.exclude(pk=exclude_id)
    return queryset.exists()


def _stash_one_time_secret(request, *, kind, connection_id, secret):
    request.session["communication_one_time_secret"] = {
        "kind": kind,
        "connection_id": connection_id,
        "secret": secret,
    }
    request.session.modified = True


def _pop_one_time_secret(request, *, kind, connection_id):
    payload = request.session.pop("communication_one_time_secret", None)
    if not isinstance(payload, dict):
        return ""
    if payload.get("kind") != kind or payload.get("connection_id") != connection_id:
        return ""
    secret = payload.get("secret")
    return secret if isinstance(secret, str) else ""


@login_required
def channels(request):
    organization = _context(request, "can_manage_channels")
    channel_items = list(
        CommunicationChannel.objects.filter(organization=organization)
        .prefetch_related("connections")
        .order_by("kind", "name", "pk")
    )
    avito_credential_ids = set(
        AvitoCredential.objects.filter(connection__channel__organization=organization)
        .values_list("connection_id", flat=True)
    )
    for channel in channel_items:
        for connection in channel.connections.all():
            connection.credentials_configured = connection.pk in avito_credential_ids
    telephony_connections = TelephonyConnection.objects.filter(
        organization=organization
    ).order_by("name", "pk")
    return render(
        request,
        "pool_service/communications/channels.html",
        {
            "active_tab": "communications",
            "channels": channel_items,
            "telephony_connections": telephony_connections,
            "communication_credential_key_configured": bool(
                getattr(settings, "COMMUNICATION_CREDENTIAL_KEY", "")
            ),
        },
    )


@login_required
@never_cache
def website_connection_create(request):
    organization = _context(request, "can_manage_channels")
    form = WebsiteConnectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        external_id = form.cleaned_data["external_id"].strip()
        if _communication_connection_exists(
            organization=organization,
            kind=CommunicationChannel.KIND_WEBSITE,
            external_id=external_id,
        ):
            form.add_error("external_id", "Такое подключение сайта уже существует.")
        else:
            try:
                with transaction.atomic():
                    channel = get_or_create_provider_channel(
                        organization, CommunicationChannel.KIND_WEBSITE
                    )
                    connection = ChannelConnection.objects.create(
                        channel=channel,
                        name=form.cleaned_data["name"].strip(),
                        external_id=external_id,
                    )
                    token = rotate_connection_token(connection)
            except IntegrityError:
                form.add_error("external_id", "Такое подключение сайта уже существует.")
            else:
                _stash_one_time_secret(
                    request,
                    kind=CommunicationChannel.KIND_WEBSITE,
                    connection_id=connection.pk,
                    secret=token,
                )
                messages.success(request, "Подключение сайта создано.")
                return redirect("communication_website_connection_edit", connection.pk)
    return render(
        request,
        "pool_service/communications/website_connection_form.html",
        {"active_tab": "communications", "form": form, "connection": None},
    )


@login_required
@never_cache
def website_connection_edit(request, connection_id):
    organization = _context(request, "can_manage_channels")
    connection = get_object_or_404(
        ChannelConnection.objects.select_related("channel"),
        pk=connection_id,
        channel__organization=organization,
        channel__kind=CommunicationChannel.KIND_WEBSITE,
    )
    initial = {"name": connection.name, "external_id": connection.external_id}
    form = WebsiteConnectionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        external_id = form.cleaned_data["external_id"].strip()
        if _communication_connection_exists(
            organization=organization,
            kind=CommunicationChannel.KIND_WEBSITE,
            external_id=external_id,
            exclude_id=connection.pk,
        ):
            form.add_error("external_id", "Такое подключение сайта уже существует.")
        else:
            connection.name = form.cleaned_data["name"].strip()
            connection.external_id = external_id
            try:
                connection.save(update_fields=["name", "external_id"])
            except IntegrityError:
                form.add_error("external_id", "Такое подключение сайта уже существует.")
            else:
                messages.success(request, "Настройки сайта сохранены.")
                return redirect("communication_website_connection_edit", connection.pk)
    one_time_secret = _pop_one_time_secret(
        request,
        kind=CommunicationChannel.KIND_WEBSITE,
        connection_id=connection.pk,
    )
    response = render(
        request,
        "pool_service/communications/website_connection_form.html",
        {
            "active_tab": "communications",
            "form": form,
            "connection": connection,
            "one_time_secret": one_time_secret,
            "public_id": str(connection.public_id),
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def website_connection_rotate_token(request, connection_id):
    organization = _context(request, "can_manage_channels")
    connection = get_object_or_404(
        ChannelConnection,
        pk=connection_id,
        channel__organization=organization,
        channel__kind=CommunicationChannel.KIND_WEBSITE,
    )
    token = rotate_connection_token(connection)
    _stash_one_time_secret(
        request,
        kind=CommunicationChannel.KIND_WEBSITE,
        connection_id=connection.pk,
        secret=token,
    )
    messages.success(request, "Токен сайта перевыпущен. Старый токен больше не действует.")
    return redirect("communication_website_connection_edit", connection.pk)


@login_required
@never_cache
def avito_connection_create(request):
    organization = _context(request, "can_manage_channels")
    form = AvitoConnectionForm(request.POST or None, require_credentials=True)
    if request.method == "POST" and form.is_valid():
        external_id = form.cleaned_data["external_id"]
        if _communication_connection_exists(
            organization=organization,
            kind=CommunicationChannel.KIND_AVITO,
            external_id=external_id,
        ):
            form.add_error("external_id", "Этот аккаунт Авито уже подключён.")
        else:
            try:
                with transaction.atomic():
                    channel = get_or_create_provider_channel(
                        organization, CommunicationChannel.KIND_AVITO
                    )
                    connection = ChannelConnection.objects.create(
                        channel=channel,
                        name=form.cleaned_data["name"].strip(),
                        external_id=external_id,
                    )
                    configure_avito_credentials(
                        connection=connection,
                        client_id=form.cleaned_data["client_id"],
                        client_secret=form.cleaned_data["client_secret"],
                    )
                    webhook_token = rotate_connection_token(connection)
            except IntegrityError:
                form.add_error("external_id", "Этот аккаунт Авито уже подключён.")
            else:
                _stash_one_time_secret(
                    request,
                    kind=CommunicationChannel.KIND_AVITO,
                    connection_id=connection.pk,
                    secret=webhook_token,
                )
                messages.success(request, "Аккаунт Авито добавлен.")
                return redirect("communication_avito_connection_edit", connection.pk)
    return render(
        request,
        "pool_service/communications/avito_connection_form.html",
        {
            "active_tab": "communications",
            "form": form,
            "connection": None,
            "communication_credential_key_configured": bool(
                getattr(settings, "COMMUNICATION_CREDENTIAL_KEY", "")
            ),
        },
    )


@login_required
@never_cache
def avito_connection_edit(request, connection_id):
    organization = _context(request, "can_manage_channels")
    connection = get_object_or_404(
        ChannelConnection.objects.select_related("channel"),
        pk=connection_id,
        channel__organization=organization,
        channel__kind=CommunicationChannel.KIND_AVITO,
    )
    credential_configured = AvitoCredential.objects.filter(connection=connection).exists()
    initial = {"name": connection.name, "external_id": connection.external_id}
    form = AvitoConnectionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        external_id = form.cleaned_data["external_id"]
        if _communication_connection_exists(
            organization=organization,
            kind=CommunicationChannel.KIND_AVITO,
            external_id=external_id,
            exclude_id=connection.pk,
        ):
            form.add_error("external_id", "Этот аккаунт Авито уже подключён.")
        else:
            try:
                with transaction.atomic():
                    connection.name = form.cleaned_data["name"].strip()
                    connection.external_id = external_id
                    connection.save(update_fields=["name", "external_id"])
                    if form.cleaned_data.get("client_id"):
                        configure_avito_credentials(
                            connection=connection,
                            client_id=form.cleaned_data["client_id"],
                            client_secret=form.cleaned_data["client_secret"],
                        )
            except IntegrityError:
                form.add_error("external_id", "Этот аккаунт Авито уже подключён.")
            else:
                messages.success(request, "Настройки Авито сохранены.")
                return redirect("communication_avito_connection_edit", connection.pk)
    one_time_secret = _pop_one_time_secret(
        request,
        kind=CommunicationChannel.KIND_AVITO,
        connection_id=connection.pk,
    )
    callback_url = ""
    if one_time_secret:
        callback_url = request.build_absolute_uri(
            reverse("avito_webhook", args=[connection.public_id, one_time_secret])
        )
    response = render(
        request,
        "pool_service/communications/avito_connection_form.html",
        {
            "active_tab": "communications",
            "form": form,
            "connection": connection,
            "credential_configured": credential_configured,
            "one_time_secret": one_time_secret,
            "callback_url": callback_url,
            "public_id": str(connection.public_id),
            "communication_credential_key_configured": bool(
                getattr(settings, "COMMUNICATION_CREDENTIAL_KEY", "")
            ),
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@login_required
@require_POST
def avito_connection_rotate_webhook(request, connection_id):
    organization = _context(request, "can_manage_channels")
    connection = get_object_or_404(
        ChannelConnection,
        pk=connection_id,
        channel__organization=organization,
        channel__kind=CommunicationChannel.KIND_AVITO,
    )
    webhook_token = rotate_connection_token(connection)
    _stash_one_time_secret(
        request,
        kind=CommunicationChannel.KIND_AVITO,
        connection_id=connection.pk,
        secret=webhook_token,
    )
    messages.success(
        request,
        "Webhook-токен Авито перевыпущен. Старый callback больше не авторизуется.",
    )
    return redirect("communication_avito_connection_edit", connection.pk)


@login_required
@never_cache
def megafon_connection_create(request):
    organization = _context(request, "can_manage_channels")
    form = MegafonConnectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        if TelephonyConnection.objects.filter(
            organization=organization,
            external_id=form.cleaned_data["external_id"].strip(),
        ).exists():
            form.add_error("external_id", "Такое подключение Мегафона уже существует.")
        else:
            try:
                connection = TelephonyConnection(
                    organization=organization,
                    name=form.cleaned_data["name"].strip(),
                    external_id=form.cleaned_data["external_id"].strip(),
                    recording_allowed_hosts=form.cleaned_data["recording_hosts"],
                )
                connection.full_clean()
                connection.save()
            except IntegrityError:
                form.add_error("external_id", "Такое подключение Мегафона уже существует.")
            else:
                messages.success(request, "Настройки Мегафона добавлены.")
                return redirect("communication_megafon_connection_edit", connection.pk)
    return render(
        request,
        "pool_service/communications/megafon_connection_form.html",
        {"active_tab": "communications", "form": form, "connection": None},
    )


@login_required
@never_cache
def megafon_connection_edit(request, connection_id):
    organization = _context(request, "can_manage_channels")
    connection = get_object_or_404(
        TelephonyConnection,
        pk=connection_id,
        organization=organization,
    )
    initial = {
        "name": connection.name,
        "external_id": connection.external_id,
        "recording_hosts": "\n".join(connection.recording_allowed_hosts or []),
    }
    form = MegafonConnectionForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        external_id = form.cleaned_data["external_id"].strip()
        if TelephonyConnection.objects.filter(
            organization=organization,
            external_id=external_id,
        ).exclude(pk=connection.pk).exists():
            form.add_error("external_id", "Такое подключение Мегафона уже существует.")
        else:
            connection.name = form.cleaned_data["name"].strip()
            connection.external_id = external_id
            connection.recording_allowed_hosts = form.cleaned_data["recording_hosts"]
            try:
                connection.full_clean()
                connection.save(
                    update_fields=["name", "external_id", "recording_allowed_hosts"]
                )
            except IntegrityError:
                form.add_error("external_id", "Такое подключение Мегафона уже существует.")
            else:
                messages.success(request, "Настройки Мегафона сохранены.")
                return redirect("communication_megafon_connection_edit", connection.pk)
    return render(
        request,
        "pool_service/communications/megafon_connection_form.html",
        {"active_tab": "communications", "form": form, "connection": connection},
    )


@login_required
@require_POST
def megafon_connection_set_active(request, connection_id):
    organization = _context(request, "can_manage_channels")
    desired = _requested_active(request)
    if desired is None:
        return HttpResponseBadRequest("Некорректный статус подключения.")
    connection = get_object_or_404(
        TelephonyConnection,
        pk=connection_id,
        organization=organization,
    )
    if connection.is_active != desired:
        connection.is_active = desired
        connection.save(update_fields=["is_active"])
    messages.success(
        request,
        "Подключение Мегафона включено." if desired else "Подключение Мегафона отключено.",
    )
    return redirect("communications_channels")


def _requested_active(request):
    value = request.POST.get("active")
    if value not in {"0", "1"}:
        return None
    return value == "1"


@login_required
@require_POST
@transaction.atomic
def communication_channel_set_active(request, channel_id):
    organization = _context(request, "can_manage_channels")
    desired = _requested_active(request)
    if desired is None:
        return HttpResponseBadRequest("Некорректный статус канала.")
    channel = get_object_or_404(
        CommunicationChannel.objects.select_for_update(),
        pk=channel_id,
        organization=organization,
    )
    if channel.is_active != desired:
        channel.is_active = desired
        channel.save(update_fields=["is_active"])
    messages.success(request, "Канал включён." if desired else "Канал отключён.")
    return redirect("communications_channels")


@login_required
@require_POST
@transaction.atomic
def communication_connection_set_active(request, connection_id):
    organization = _context(request, "can_manage_channels")
    desired = _requested_active(request)
    if desired is None:
        return HttpResponseBadRequest("Некорректный статус подключения.")
    connection = get_object_or_404(
        ChannelConnection.objects.select_for_update().select_related("channel"),
        pk=connection_id,
        channel__organization=organization,
    )
    if connection.is_active != desired:
        connection.is_active = desired
        connection.save(update_fields=["is_active"])
    messages.success(request, "Подключение включено." if desired else "Подключение отключено.")
    return redirect("communications_channels")


@login_required
@ensure_csrf_cookie
def communication_notification_feed(request):
    organization = _context(request, "can_view_conversations")
    visible_ids = []
    raw_visible = request.GET.get("visible", "")
    if raw_visible:
        parts = raw_visible.split(",")
        if (
            len(raw_visible) > 500
            or len(parts) > 50
            or any(not item.isascii() or not item.isdecimal() for item in parts)
        ):
            return JsonResponse({"error": "invalid_visible"}, status=400)
        visible_ids = [int(item) for item in parts]
        if any(item <= 0 or item > 9_223_372_036_854_775_807 for item in visible_ids):
            return JsonResponse({"error": "invalid_visible"}, status=400)
    notifications = list(Notification.objects.filter(
        user=request.user,
        organization=organization,
        kind="communication",
        is_resolved=False,
    ).order_by("-created_at", "-pk")[:10])
    result = []
    for notification in reversed(notifications):
        action_url = notification.action_url
        if not action_url.startswith("/communications/") or action_url.startswith("//"):
            action_url = reverse("communications_conversations")
        result.append({
            "id": notification.pk,
            "title": notification.title,
            "message": notification.message,
            "action_url": action_url,
            "created_at": notification.created_at.isoformat(),
        })
    resolved_ids = list(Notification.objects.filter(
        user=request.user,
        organization=organization,
        kind="communication",
        is_resolved=True,
        pk__in=visible_ids,
    ).values_list("pk", flat=True))
    return JsonResponse({"notifications": result, "resolved_ids": resolved_ids})


@login_required
@require_POST
def communication_notification_resolve(request, notification_id):
    organization = _context(request, "can_view_conversations")
    notification = get_object_or_404(
        Notification,
        pk=notification_id,
        user=request.user,
        organization=organization,
        kind="communication",
        is_resolved=False,
    )
    notification.is_read = True
    notification.is_resolved = True
    notification.resolved_at = timezone.now()
    notification.save(update_fields=["is_read", "is_resolved", "resolved_at"])
    return JsonResponse({"resolved": True})
