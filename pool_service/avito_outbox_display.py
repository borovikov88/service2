"""Read-only Avito messaging labels; callers must already enforce organization access."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .communication_models import CommunicationChannel, ConversationMessage

WORKER_LABELS = {
    "disabled": "Автоматическая отправка не включена",
    "never_run": "Обработчик ещё не запускался",
    "running": "Обработчик выполняется",
    "healthy": "Последний запуск обработчика успешен",
    "stale": "Нет свежего подтверждения работы обработчика",
    "error": "Последний запуск обработчика завершился ошибкой",
}
PERIODIC_LABELS = {
    "disabled": "Резервная обработка: не включена",
    "never_run": "Резервная обработка: ещё не подтверждена",
    "running": "Резервная обработка: выполняется",
    "healthy": "Резервная обработка: последний запуск успешен",
    "stale": "Нет свежего подтверждения периодической обработки очереди",
    "error": "Последняя периодическая обработка завершилась ошибкой",
    "unknown": "Результат периодической обработки не подтверждён",
}
IMMEDIATE_LABELS = {
    "disabled": "не включена", "never_run": "первый отдельный запуск ещё не зафиксирован",
    "running": "выполняется", "healthy": "последний запуск успешен",
    "error": "последний запуск завершился ошибкой", "unknown": "результат не подтверждён",
}
ERROR_LABELS = {
    "provider_delivery_unknown": "Авито мог принять сообщение. Результат отправки неизвестен; повтор заблокирован до сверки.",
    "provider_message_id_missing": "В ответе Авито нет подтверждённого ID сообщения. Результат отправки неизвестен.",
    "provider_identifier_too_long": "ID в ответе Авито не прошёл проверку. Результат отправки неизвестен.",
    "provider_invalid_json": "Ответ Авито не удалось проверить. Результат отправки неизвестен.",
    "provider_invalid_response": "Ответ Авито не удалось проверить. Результат отправки неизвестен.",
    "provider_response_too_large": "Ответ Авито не прошёл проверку размера. Результат отправки неизвестен.",
    "provider_unavailable": "Не удалось обратиться к Авито. Проверьте состояние обработчика и попытки.",
    "attachments_require_provider_upload": "Отправка вложений через Авито пока не подключена.",
    "empty_message": "Текст сообщения пуст.",
    "connection_inactive": "Подключение или канал выключены.",
    "account_mismatch": "Текущая привязка аккаунта не подтверждена.",
    "outbox_binding_changed": "Привязка подключения изменилась. Отправка остановлена.",
    "sender_access_denied": "У автора ответа больше нет права отправки.",
    "legacy_message_held": "Старое сообщение удерживается до отдельной проверки и согласования отправки.",
    "outbox_disabled": "Автоматическая отправка для подключения не включена.",
    "outbox_sender_access_revoked": "У автора ответа больше нет права отправки.",
    "outbox_scope_invalid": "Привязка аккаунта или организации не прошла проверку. Отправка остановлена.",
    "outbox_legacy_held": "Старое сообщение удерживается до отдельной проверки и согласования отправки.",
    "provider_reconcile_ambiguous": "Найдены неоднозначные результаты сверки с Авито. Повтор отправки заблокирован.",
    "provider_reconcile_incomplete": "Проверка истории Авито неполна. Безопасность повтора не подтверждена.",
    "provider_reconcile_invalid": "Результат сверки с Авито не прошёл проверку. Повтор отправки заблокирован.",
}
UNKNOWN_ERROR = "Причина не распознана. Нужна проверка обработчика; полный ответ провайдера здесь не выводится."


def error_label(code):
    """Never turn arbitrary persisted provider/error text into visible diagnostics."""
    if not code:
        return ""
    if not isinstance(code, str):
        return UNKNOWN_ERROR
    if code in ERROR_LABELS:
        return ERROR_LABELS[code]
    if code == "provider_http_401":
        return "Авито отказал в авторизации (HTTP 401). Требуется проверить доступ подключения."
    if code == "provider_http_402":
        return "Авито отклонил запрос с HTTP 402. Условия оплаты или тарифа требуют подтверждения по ответу Авито."
    if code == "provider_http_403":
        return "Авито запретил запрос (HTTP 403). Причина — тариф, права или авторизация — пока не подтверждена."
    if code == "provider_http_429":
        return "Авито ограничил частоту запросов (HTTP 429)."
    if code.startswith("provider_http_") and len(code) == 17 and code[-3:].isdigit():
        return "Авито вернул HTTP " + code[-3:] + "."
    return UNKNOWN_ERROR


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _time(value):
    try:
        parsed = value if isinstance(value, datetime) else parse_datetime(value) if isinstance(value, str) else None
    except (ValueError, TypeError, OverflowError):
        parsed = None
    if parsed is None or timezone.is_naive(parsed):
        return ""
    return timezone.localtime(parsed, ZoneInfo("Asia/Barnaul")).strftime("%d.%m.%Y %H:%M:%S")


def _age(value):
    value = _count(value)
    if value is None:
        return ""
    if value < 60:
        return str(value) + " сек."
    if value < 3600:
        return str(value // 60) + " мин."
    return str(value // 3600) + " ч. " + str((value % 3600) // 60) + " мин."


def _configuration(connection, user, raw):
    if (user is None or not getattr(user, "is_authenticated", False)
            or connection.pk != 4 or str(connection.external_id) != "446312402"
            or raw.get("enabled") is not True or raw.get("binding_valid") is not True):
        return None
    from .communication_services import conversation_capability, organization_access
    organization = connection.channel.organization
    if (organization_access(user, organization) is None
            or not conversation_capability(user, "can_manage_channels", organization)):
        return None
    try:
        from scripts.avito_outbox_cron import panel_command
        command = panel_command(Path(settings.BASE_DIR).resolve())
        if not isinstance(command, str) or not command:
            raise ValueError("panel_command_invalid")
    except Exception:
        # Operational paths and exception messages stay out of the response.
        return {"available": False}
    return {"available": True, "schedule": "* * * * *", "command": command}


def for_connection(connection, user=None):
    """No provider requests or settings writes; one scoped DB queue snapshot."""
    if connection.channel.kind != CommunicationChannel.KIND_AVITO:
        return None
    from .avito_outbox import outbox_diagnostics
    from .avito_webhook_chain import display
    raw = outbox_diagnostics(connection)
    raw = raw if isinstance(raw, dict) else {}
    metadata = connection.settings if isinstance(connection.settings, dict) else {}
    rows = metadata.get("avito_api_results")
    bound = str(metadata.get("avito_api_account_id", "")) == str(connection.external_id)
    reading = next((row for row in rows if isinstance(row, dict) and row.get("key") == "messenger"), None) if bound and isinstance(rows, list) else None
    read_status = reading.get("status") if reading else None
    read_status = read_status if isinstance(read_status, str) else None
    read_label = {
        "ok": "Чтение чатов подтверждено последней проверкой",
        "denied": "Последняя проверка чтения чатов: нет доступа",
        "error": "Последняя проверка чтения чатов завершилась ошибкой",
    }.get(read_status, "Доступ к чтению чатов не подтверждён")
    chain = display(connection)
    webhook = metadata.get("avito_webhook_status")
    webhook = webhook if isinstance(webhook, str) else None
    registration_bound = chain.get("state") in {"awaiting_first", "proof_invalid", "persisted_row_missing", "accepted_persisted"}
    if webhook == "connected":
        webhook_label = ("Подписка зарегистрирована на текущей привязке" if registration_bound else
                         "Сохранена регистрация; текущая привязка не подтверждена")
    else:
        webhook_label = {"error": "Ошибка регистрации подписки",
                         "not_connected": "Подписка не зарегистрирована"}.get(
                             webhook, "Регистрация подписки не подтверждена")
    worker_state = raw.get("worker_state")
    worker_state = worker_state if isinstance(worker_state, str) else None
    periodic_state = raw.get("periodic_state")
    periodic_state = periodic_state if isinstance(periodic_state, str) and periodic_state in PERIODIC_LABELS else "unknown"
    immediate_state = raw.get("immediate_state")
    immediate_state = immediate_state if isinstance(immediate_state, str) and immediate_state in IMMEDIATE_LABELS else "unknown"
    history = raw.get("periodic_runs")
    history = history if isinstance(history, list) else []
    periodic_runs = []
    for row in history[-4:]:
        if (not isinstance(row, dict) or not isinstance(row.get("state"), str)
                or row["state"] not in {"success", "error", "running", "unknown"}):
            continue
        result = row.get("result")
        before, after = row.get("queue_before"), row.get("queue_after")
        periodic_runs.append({
            "started_at": _time(row.get("started_at")), "completed_at": _time(row.get("completed_at")),
            "label": {"success": "завершён успешно", "error": "завершён с ошибкой",
                      "running": "выполняется", "unknown": "завершение не подтверждено"}[row["state"]],
            "empty_queue": (isinstance(before, dict) and isinstance(after, dict)
                            and all(type(value.get(key)) is int and value[key] == 0
                                    for value in (before, after) for key in ("pending", "unknown", "failed"))),
            "accepted": _count(result.get("accepted")) if isinstance(result, dict) else None,
        })
    return {
        "worker_state": worker_state if worker_state in WORKER_LABELS else "unknown",
        "worker_label": PERIODIC_LABELS[periodic_state],
        "worker_warning": periodic_state != "healthy",
        "periodic_state": periodic_state, "immediate_state": immediate_state,
        "immediate_label": IMMEDIATE_LABELS[immediate_state],
        "last_immediate_started_at": _time(raw.get("last_immediate_started_at")),
        "last_immediate_completed_at": _time(raw.get("last_immediate_completed_at")),
        "last_immediate_success_at": _time(raw.get("last_immediate_success_at")),
        "last_immediate_error_label": error_label(raw.get("last_immediate_error_code")),
        "last_periodic_completed_at": _time(raw.get("last_periodic_completed_at")),
        "last_periodic_error_label": error_label(raw.get("last_periodic_error_code")),
        "periodic_runs": periodic_runs,
        "last_started_at": _time(raw.get("last_started_at")),
        "last_success_at": _time(raw.get("last_success_at")),
        "post_commit_enabled": raw.get("enabled") is True and raw.get("binding_valid") is True,
        "last_periodic_started_at": _time(raw.get("last_periodic_started_at")),
        "last_periodic_success_at": _time(raw.get("last_periodic_success_at")),
        "configuration": _configuration(connection, user, raw),
        "last_error_label": error_label(raw.get("last_error_code")),
        "pending_count": _count(raw.get("pending_count")),
        "unknown_count": _count(raw.get("unknown_count")),
        "failed_count": _count(raw.get("failed_count")),
        "legacy_held_count": _count(raw.get("legacy_held_count")),
        "oldest_pending_at": _time(raw.get("oldest_pending_at")),
        "oldest_pending_age": _age(raw.get("oldest_pending_age_seconds")),
        "read_label": read_label,
        "read_checked_at": _time(metadata.get("avito_api_checked_at")) if reading else "",
        "webhook_label": webhook_label,
        "inbound_label": chain["state_label"],
        "inbound_at": _time(chain.get("received_at")),
        "owner_notification_label": chain["owner_notification_label"],
        "owner_notification_recorded": chain.get("owner_notification_recorded") is True,
    }


def message_display(message):
    state = message.delivery_status
    code = message.delivery_error
    if state == ConversationMessage.DELIVERY_DELIVERED and message.external_id:
        label, detail = "Принято Авито", "Авито подтвердил ID сообщения. Прочтение клиентом не подтверждено."
    elif state == ConversationMessage.DELIVERY_DELIVERED:
        label, detail = "Отправка не подтверждена", "Нет подтверждённого ID сообщения Авито."
    elif state == ConversationMessage.DELIVERY_SENDING:
        label, detail = "Результат отправки неизвестен", "Повтор заблокирован до сверки с Авито."
    elif state == ConversationMessage.DELIVERY_FAILED:
        label, detail = "Ошибка отправки", error_label(code) or "Причина ошибки не записана; требуется проверка."
    elif state == ConversationMessage.DELIVERY_PENDING:
        label, detail = "Ожидает отправки", error_label(code) or "Сообщение сохранено в очереди. Состояние обработчика показано выше."
    else:
        label, detail = "Отправка не подтверждена", "Нет подтверждённого результата отправки."
    return {"label": label, "detail": detail, "attempts": _count(message.delivery_attempts),
            "accepted_at": _time(message.delivered_at) if state == ConversationMessage.DELIVERY_DELIVERED and message.external_id else ""}


def conversation_context(selected, organization, user=None):
    """The view has checked capability; still reject any cross-org relation."""
    context = {"selected_messages": [], "avito_outbox_state": None}
    if selected is None or selected.organization_id != organization.pk or selected.connection.channel.organization_id != organization.pk:
        return context
    messages = list(selected.messages.prefetch_related("attachments"))
    if selected.connection.channel.kind == CommunicationChannel.KIND_AVITO:
        context["avito_outbox_state"] = for_connection(selected.connection, user=user)
        for message in messages:
            if message.direction == ConversationMessage.DIRECTION_OUT:
                message.avito_delivery = message_display(message)
    context["selected_messages"] = messages
    return context
