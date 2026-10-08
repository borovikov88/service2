"""Read-only Avito data and API diagnostics using existing credentials.

Explicit CSRF-protected refreshes retain bounded, allowlisted display fields.
Raw provider bodies, message content and unencrypted credentials are never saved.
"""
import re
import uuid
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlencode

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from pool_service.communication_avito import (
    AvitoError,
    _json_request,
    _provider_root,
    access_token,
    authorized_account_id,
    verify_messenger_access,
    webhook_subscriptions,
)
from pool_service.communication_models import (
    AvitoCredential, ChannelConnection, CommunicationChannel, Conversation, ConversationMessage,
)
from pool_service import avito_workspace
from pool_service.communication_services import conversation_capability, organization_access
from pool_service.communication_views import _avito_subscription_token


CHECKS = (
    ("token", "Авторизация"),
    ("account", "Аккаунт"),
    ("messenger", "Чаты / Messenger"),
    ("webhook", "Подписка на сообщения"),
    ("items", "Объявления"),
    ("balance", "Баланс"),
    ("autoload", "Автозагрузка v4"),
)

SAFE_ERRORS = {
    "provider_data_invalid": "Авито вернул неожиданный формат данных.",
    "provider_account_mismatch": "ID аккаунта по ключам отличается от подключения. Проверьте настройки.",
    "provider_credentials_missing": "Ключи не сохранены.",
    "provider_credentials_invalid": "Не удалось расшифровать ключи.",
    "provider_token_missing": "Авито не вернул access token.",
    "provider_account_id_missing": "Авито не вернул ID аккаунта.",
    "provider_account_id_invalid": "Некорректный ID в ответе Авито.",
    "provider_chats_invalid": "Неожиданный формат ответа чатов.",
    "provider_subscriptions_invalid": "Неожиданный ответ списка подписок.",
    "provider_invalid_response": "Некорректный ответ Авито.",
    "provider_invalid_json": "Ответ Авито не является JSON.",
    "provider_response_too_large": "Превышен лимит ответа.",
    "provider_unavailable": "Авито недоступен или превышено время ожидания.",
}


def _scope(request):
    access = organization_access(request.user)
    if not access or not conversation_capability(
        request.user, "can_manage_channels", access.organization
    ):
        raise PermissionDenied
    return access.organization


def _entry(key, status, detail, code=""):
    return {
        "key": key,
        "label": dict(CHECKS)[key],
        "status": status,
        "detail": detail[:160],
        "code": code,
    }


def _failure(key, exc):
    # Only allow fixed provider error identifiers; never persist exception text,
    # HTTP bodies, Authorization headers or URLs from remote services.
    code = str(exc)
    if re.fullmatch(r"provider_http_[1-5][0-9]{2}", code):
        status_code = int(code.rsplit("_", 1)[1])
        if status_code in (401, 402, 403):
            return _entry(
                key, "denied",
                f"HTTP {status_code}: проверьте разрешения API и условия тарифа.",
                code,
            )
        if status_code == 404:
            return _entry(key, "warning", "HTTP 404: метод или данные не найдены.", code)
        if status_code == 429:
            return _entry(key, "warning", "HTTP 429: слишком много запросов.", code)
        return _entry(key, "error", f"Авито вернул HTTP {status_code}.", code)
    if code in SAFE_ERRORS:
        return _entry(key, "error", SAFE_ERRORS[code], code)
    return _entry(key, "error", "Не удалось выполнить проверку.", "provider_error")


def _probe(key, callback):
    try:
        return _entry(key, "ok", callback() or "Доступ подтверждён.")
    except AvitoError as exc:
        return _failure(key, exc)


def _last_successful_upload(token):
    _json_request(
        f"{_provider_root()}/autoload/v4/uploads/last_successful",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=8,
    )
    return "Метод автозагрузки v4 доступен."


def _items_access(token):
    response = _json_request(
        f"{_provider_root()}/core/v1/items?per_page=1&page=1",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=8,
    )
    meta = response.get("meta")
    total = meta.get("total") if isinstance(meta, dict) else None
    if type(total) is int and 0 <= total <= 10_000_000:
        return f"Доступ подтверждён. Всего объявлений в ответе: {total}."
    return "Получение объявлений доступно."


def _balance_access(token, account_id):
    _json_request(
        f"{_provider_root()}/core/v1/accounts/{quote(account_id, safe='')}/balance/",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        timeout=8,
    )
    return "Метод баланса доступен. Суммы не сохраняются."


def _webhook_access(connection, request):
    urls = webhook_subscriptions(connection)
    if any(
        secret and connection.check_api_token(secret)
        for secret in (_avito_subscription_token(request, connection, url) for url in urls)
    ):
        return "Webhook Service2 найден в подписках Авито."
    # A valid API response does not mean the webhook belongs to Service2.
    return None


def diagnose(connection, request):
    """Probe current credentials and selected API families; never modify Avito."""
    results = []
    try:
        token = access_token(connection)
    except AvitoError as exc:
        results.append(_failure("token", exc))
        results.extend(_entry(key, "skipped", "Не проверено: нет авторизации.")
                       for key, _ in CHECKS[1:])
        return {"account_id": "", "results": results}
    results.append(_entry("token", "ok", "Токен получен или обновлён."))

    try:
        actual_id = authorized_account_id(connection)
    except AvitoError as exc:
        results.append(_failure("account", exc))
        results.extend(_entry(key, "skipped", "Не проверено: неизвестен аккаунт.")
                       for key, _ in CHECKS[2:])
        return {"account_id": "", "results": results}

    matches = str(connection.external_id) == actual_id
    results.append(_entry(
        "account", "ok" if matches else "warning",
        "ID совпадает с настройкой Service2." if matches
        else "ID этих ключей НЕ совпадает с ID подключения. Перепроверьте аккаунт.",
    ))

    results.append(_probe(
        "messenger", lambda: (
            verify_messenger_access(connection, actual_id)
            and "Чтение списка чатов доступно."
        ),
    ))
    try:
        subscribed = _webhook_access(connection, request)
        results.append(_entry(
            "webhook",
            "ok" if subscribed else "warning",
            subscribed or "API отвечает, но действующий webhook Service2 не найден.",
        ))
    except AvitoError as exc:
        results.append(_failure("webhook", exc))

    results.append(_probe("items", lambda: _items_access(token)))
    results.append(_probe("balance", lambda: _balance_access(token, actual_id)))
    results.append(_probe("autoload", lambda: _last_successful_upload(token)))
    return {"account_id": actual_id, "results": results}


@login_required
@never_cache
def avito_dashboard(request):
    organization = _scope(request)
    connections = list(
        ChannelConnection.objects.filter(
            channel__organization=organization,
            channel__kind=CommunicationChannel.KIND_AVITO,
        ).select_related("channel").order_by("name", "pk")
    )
    configured_ids = set(
        AvitoCredential.objects.filter(connection_id__in=[item.pk for item in connections])
        .values_list("connection_id", flat=True)
    )
    accounts = []
    checked_count = 0
    for connection in connections:
        data = connection.settings if isinstance(connection.settings, dict) else {}
        checked_at = data.get("avito_api_checked_at") or ""
        if checked_at:
            checked_count += 1
        # Older/malformed metadata must not break the dashboard or become HTML.
        results = data.get("avito_api_results", [])
        if not isinstance(results, list):
            results = []
        accounts.append({
            "connection": connection,
            "has_credentials": connection.pk in configured_ids,
            "checked_at": checked_at,
            "results": [
                row for row in results
                if isinstance(row, dict) and row.get("key") in dict(CHECKS)
            ][:len(CHECKS)],
            "actual_id": data.get("avito_api_account_id", ""),
            "webhook_status": data.get("avito_webhook_status", "not_connected"),
            "webhook_received": data.get("avito_webhook_last_received_at", ""),
            "webhook_error": data.get("avito_webhook_error", ""),
            "pull_error": data.get("avito_pull_last_error", ""),
        })
    requested_id = request.GET.get("account", "")
    selected = next((item for item in accounts if str(item["connection"].pk) == requested_id), None)
    if requested_id and selected is None:
        raise Http404
    selected = selected or (accounts[0] if accounts else None)
    workspace = {}
    if selected:
        connection = selected["connection"]
        metadata = connection.settings if isinstance(connection.settings, dict) else {}
        saved = metadata.get("avito_workspace", {})
        if isinstance(saved, dict) and saved.get("account_id") == str(connection.external_id):
            workspace = {key: value for key, value in saved.get("sections", {}).items()
                         if key in avito_workspace.SECTIONS and isinstance(value, dict)} if isinstance(saved.get("sections"), dict) else {}
    local_analytics = _local_analytics(request, selected["connection"] if selected else None)
    return render(request, "pool_service/avito/dashboard.html", {
        "active_tab": "avito",
        "selected": selected,
        "workspace": workspace,
        "local_analytics": local_analytics,
        "accounts": accounts,
        "connected_count": len(accounts),
        "configured_count": len(configured_ids),
        "checked_count": checked_count,
    })


@login_required
@require_POST
@never_cache
def avito_check_api(request, connection_id):
    organization = _scope(request)
    connection = get_object_or_404(
        ChannelConnection.objects.select_related("channel"),
        pk=connection_id,
        channel__organization=organization,
        channel__kind=CommunicationChannel.KIND_AVITO,
    )
    report = diagnose(connection, request)
    # Update only our own metadata keys, while preserving webhook settings.
    # No raw provider payloads, tokens, chats, PII, or webhook URLs are saved.
    with transaction.atomic():
        locked = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection.pk)
        data = dict(locked.settings or {})
        data["avito_api_checked_at"] = timezone.now().isoformat()
        data["avito_api_results"] = report["results"]
        data["avito_api_account_id"] = report["account_id"]
        locked.settings = data
        locked.save(update_fields=["settings"])
    messages.info(request, "Проверка API Авито завершена. Результаты приведены ниже.")
    return redirect("avito_dashboard")


class WorkspaceRefreshForm(forms.Form):
    section = forms.ChoiceField(choices=[(x, x) for x in ("all", *avito_workspace.SECTIONS)])
    page = forms.IntegerField(min_value=1, max_value=10000, required=False, initial=1)
    status = forms.ChoiceField(choices=[("", "Все"), *avito_workspace.ITEM_STATUSES.items()], required=False)


class AnalyticsPeriodForm(forms.Form):
    date_from = forms.DateField(required=False)
    date_to = forms.DateField(required=False)


def _local_analytics(request, connection):
    # Provider event timestamps aren't present on all imported messages. These
    # counts intentionally use local creation/receipt times, not Avito lead dates.
    zone = ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "UTC"))
    today = timezone.localdate(timezone=zone)
    start, end = today - timedelta(days=29), today
    form = AnalyticsPeriodForm(request.GET)
    period_error = ""
    if form.is_valid():
        requested_start = form.cleaned_data.get("date_from") or start
        requested_end = form.cleaned_data.get("date_to") or end
        if requested_start <= requested_end <= today and (requested_end - requested_start).days <= 365:
            start, end = requested_start, requested_end
        else:
            period_error = "Выберите период до 366 дней, не позднее сегодняшнего дня. Показаны последние 30 дней."
    else:
        period_error = "Некорректные даты. Показаны последние 30 дней."
    result = {"start": start.isoformat(), "end": end.isoformat(), "timezone": str(zone),
              "period_error": period_error, "conversations": 0, "inbound": 0, "outbound": 0,
              "open_conversations": 0, "available": False}
    if connection is None or not conversation_capability(request.user, "can_view_conversations", connection.channel.organization):
        return result
    result["available"] = True
    begin = datetime.combine(start, time.min, tzinfo=zone)
    finish = datetime.combine(end + timedelta(days=1), time.min, tzinfo=zone)
    conversations = Conversation.objects.filter(
        connection=connection, organization=connection.channel.organization,
    )
    result["conversations"] = conversations.filter(created_at__gte=begin, created_at__lt=finish).count()
    result["open_conversations"] = conversations.filter(status__in=("new", "active", "waiting")).count()
    counts = ConversationMessage.objects.filter(
        conversation__in=conversations, created_at__gte=begin, created_at__lt=finish,
    ).aggregate(
        inbound=Count("pk", filter=Q(direction=ConversationMessage.DIRECTION_IN)),
        outbound=Count("pk", filter=Q(direction=ConversationMessage.DIRECTION_OUT,
                                      delivery_status=ConversationMessage.DELIVERY_DELIVERED)),
    )
    result.update(counts)
    return result


def _workspace_failure(section, exc):
    check_key = "account" if section == "profile" else section
    failure = _failure(check_key, exc)
    return {key: failure[key] for key in ("status", "detail", "code")}


@login_required
@require_POST
@never_cache
def avito_refresh_data(request, connection_id):
    organization = _scope(request)
    connection = get_object_or_404(
        ChannelConnection.objects.select_related("channel"), pk=connection_id,
        channel__organization=organization, channel__kind=CommunicationChannel.KIND_AVITO,
    )
    redirect_params = {"account": connection.pk}
    for key in ("date_from", "date_to", "q"):
        value = request.POST.get(key, "")
        if value:
            redirect_params[key] = value[:200 if key == "q" else 10]
    destination = reverse("avito_dashboard") + "?" + urlencode(redirect_params)
    form = WorkspaceRefreshForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Некорректные параметры обновления Авито.")
        return redirect(destination)
    section = form.cleaned_data["section"]
    targets = avito_workspace.SECTIONS if section == "all" else (section,)
    page = form.cleaned_data["page"] or 1
    status = form.cleaned_data["status"]
    now = timezone.now()
    lease = uuid.uuid4().hex
    with transaction.atomic():
        locked = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection.pk)
        if locked.channel.organization_id != organization.pk or locked.channel.kind != CommunicationChannel.KIND_AVITO:
            raise Http404
        metadata = dict(locked.settings) if isinstance(locked.settings, dict) else {}
        until = metadata.get("avito_workspace_refresh_until")
        try:
            until = parse_datetime(until) if isinstance(until, str) else None
        except ValueError:
            until = None
        if until and timezone.is_aware(until) and until > now:
            messages.info(request, "Обновление этого аккаунта уже выполняется. Дождитесь результата.")
            return redirect(destination)
        metadata["avito_workspace_refresh_until"] = (now + timedelta(minutes=3)).isoformat()
        metadata["avito_workspace_refresh_lease"] = lease
        locked.settings = metadata
        locked.save(update_fields=["settings"])

    updates = {}
    persisted = False
    actual_id = ""
    try:
        token = access_token(connection)
        profile = avito_workspace._get(token, "/core/v1/accounts/self")
        actual_id = avito_workspace.profile_data(profile)["id"]
        if actual_id != str(connection.external_id):
            raise AvitoError("provider_account_mismatch")
        for key in targets:
            checked_at = timezone.now().isoformat()
            try:
                data = avito_workspace.fetch_section(key, token, actual_id, page=page, status=status, profile=profile)
                updates[key] = {"status": "ok", "detail": "", "code": "", "data": data,
                                "checked_at": checked_at, "success_at": checked_at, "stale": False}
            except AvitoError as exc:
                updates[key] = {**_workspace_failure(key, exc), "checked_at": checked_at, "stale": True}
    except AvitoError as exc:
        for key in targets:
            updates[key] = {**_workspace_failure(key, exc), "checked_at": timezone.now().isoformat(), "stale": True}
    finally:
        # Do not overwrite a newer request or a concurrently edited account.
        with transaction.atomic():
            locked = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection.pk)
            metadata = dict(locked.settings) if isinstance(locked.settings, dict) else {}
            if metadata.get("avito_workspace_refresh_lease") == lease:
                metadata.pop("avito_workspace_refresh_lease", None)
                metadata.pop("avito_workspace_refresh_until", None)
                if (str(locked.external_id) == str(connection.external_id)
                        and locked.channel_id == connection.channel_id
                        and locked.channel.organization_id == organization.pk):
                    saved = metadata.get("avito_workspace", {})
                    if not isinstance(saved, dict) or saved.get("account_id") != str(connection.external_id):
                        saved = {"version": 1, "account_id": str(connection.external_id), "sections": {}}
                    sections = saved.get("sections")
                    sections = dict(sections) if isinstance(sections, dict) else {}
                    for key, update in updates.items():
                        previous = sections.get(key)
                        previous = previous if isinstance(previous, dict) else {}
                        if update.get("status") != "ok":
                            # The old payload keeps its original query, page and
                            # success time. A failed refresh never becomes zero.
                            update = {**previous, **update}
                        sections[key] = update
                    saved["sections"] = sections
                    metadata["avito_workspace"] = saved
                    persisted = True
                else:
                    messages.warning(request, "Подключение изменилось во время обновления; результат не сохранён.")
                locked.settings = metadata
                locked.save(update_fields=["settings"])
    if not persisted:
        messages.info(request, "Результат не сохранён: подключение или запрос обновления изменились.")
        return redirect(destination)
    successes = sum(item.get("status") == "ok" for item in updates.values())
    if successes == len(targets):
        messages.success(request, "Данные Авито обновлены.")
    elif successes:
        messages.warning(request, "Данные обновлены частично. Ошибки показаны в соответствующих разделах.")
    else:
        messages.warning(request, "Обновление не удалось. Последние успешные данные сохранены, если они были.")
    return redirect(destination)
