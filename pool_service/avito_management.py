"""Read-only Avito API availability dashboard using existing Communication credentials.

The checks run only on an explicit, CSRF-protected request. No Avito source
objects, message contents, credentials or provider response bodies are persisted.
"""
import re
from urllib.parse import quote

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
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
from pool_service.communication_models import AvitoCredential, ChannelConnection, CommunicationChannel
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
        data = connection.settings or {}
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
    return render(request, "pool_service/avito/dashboard.html", {
        "active_tab": "avito",
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
        locked = ChannelConnection.objects.select_for_update().get(pk=connection.pk)
        data = dict(locked.settings or {})
        data["avito_api_checked_at"] = timezone.now().isoformat()
        data["avito_api_results"] = report["results"]
        data["avito_api_account_id"] = report["account_id"]
        locked.settings = data
        locked.save(update_fields=["settings"])
    messages.info(request, "Проверка API Авито завершена. Результаты приведены ниже.")
    return redirect("avito_dashboard")
