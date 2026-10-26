"""Scoped evidence for completed Avito worker checks; no provider requests."""
import re
from datetime import timedelta

from django.db import transaction, DatabaseError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .communication_models import AvitoSchedulerHeartbeat, AvitoStatusMonitor, ChannelConnection

CHECKS = {"statuses": "Статусы объявлений", "advance": "Аванс",
          "autoload_report": "Подробный отчёт автозагрузки", "tariff": "Срок тарифа", "publication_correlation": "Связь версии с загрузкой",
          "messages_digest": "Отложенные сообщения владельцу", "statistics_daily": "Суточная история эффективности"}
KEYS = {"advance": "avito_advance_monitor", "autoload_report": "avito_autoload_auto",
        "tariff": "avito_tariff_reminder", "publication_correlation": "avito_autoload_auto",
        "messages_digest": "avito_deferred_owner_messages", "statistics_daily": "avito_daily_statistics_v1"}
CODES = {
    "", "not_configured", "disabled", "not_checked", "check_due", "schedule_invalid",
    "binding_changed", "check_error_unclassified", "worker_check_exception",
    "budget_or_batch_deferred", "workspace_busy", "worker_result_unknown", "quiet_hours", "not_due",
    "advance_binding_changed", "advance_data_invalid", "advance_provider_invalid", "advance_read_failed",
    "autoload_auto_binding_changed", "autoload_auto_error", "autoload_report_invalid",
    "autoload_report_changed", "autoload_report_limit", "autoload_report_duplicate_rows",
    "autoload_report_pagination", "provider_cooldown", "provider_unavailable",
    "provider_credentials_missing", "provider_credentials_invalid", "provider_token_missing",
    "provider_account_mismatch", "provider_account_id_invalid", "provider_account_id_missing",
    "provider_invalid_json", "provider_invalid_response", "provider_response_too_large",
    "monitor_scan_limit", "monitor_pagination_invalid", "monitor_status_invalid",
    "monitor_owner_unavailable", "monitor_connection_changed", "monitor_interrupted", "monitor_error",
    "tariff_reminder_failed", "tariff_notification_preferences_disabled", "notification_preferences_disabled",
    "statistics_binding_changed", "statistics_data_invalid", "statistics_total_unavailable", "statistics_limit",
    "statistics_unavailable", "statistics_incomplete", "publication_correlation_failed", "dependency_failed", "deferred_queue_invalid", "conversation_access_denied",
}
CONSISTENCY_GUARDS = {
    "queried_rows_changed": "Строки выборки по XML ID отличаются от проверочной страницы.",
    "shop_xml_snapshot_changed": "XML магазина изменился или стал непроверяемым во время чтения.",
    "profile_snapshot_changed": "Настройки источника автозагрузки изменились во время чтения.",
    "summary_snapshot_changed": "Сводка загрузки изменилась между началом и концом чтения.",
}


def consistency_guard(value):
    return value if isinstance(value, str) and value in CONSISTENCY_GUARDS else None


OUTCOMES = {"baseline", "success", "failed", "skipped", "not_run"}


def safe_code(value, *, check=None):
    return value if isinstance(value, str) and (value in CODES or
        check == "statistics_daily" and value in {"provider_data_invalid", "statistics_fields_unavailable", "statistics_provider_error"} or
        re.fullmatch(r"provider_http_[1-5][0-9]{2}", value)) else "check_error_unclassified"


def when(value):
    try:
        parsed = parse_datetime(value) if isinstance(value, str) else value
        return parsed if parsed and timezone.is_aware(parsed) else None
    except (ValueError, TypeError, AttributeError):
        return None


def worker_context():
    """The supervisor begins this shared row before starting its locked child."""
    import os
    if os.environ.get("SERVICE2_AVITO_CRON_SUPERVISED") != "1":
        return None
    heartbeat = AvitoSchedulerHeartbeat.objects.filter(pk="avito-status-monitor", state="running").first()
    now = timezone.now()
    if heartbeat and heartbeat.started_at <= now < heartbeat.started_at + timedelta(minutes=35):
        return str(heartbeat.run_id)
    return None


def record(connection_id, check, outcome, *, run_id, exception=False, deferred=False, reason=None):
    """Only a live matching supervisor row can receive worker evidence."""
    if not run_id or check not in CHECKS or outcome not in OUTCOMES:
        return
    connection = ChannelConnection.objects.select_related("channel").filter(pk=connection_id).first()
    if not connection or connection.channel.kind != "avito":
        return
    # Codes are drawn from scoped state, never from exception strings.
    state = _saved(connection, check)
    code = (reason if reason is not None and safe_code(reason, check=check) == reason else "worker_check_exception" if exception else "budget_or_batch_deferred" if deferred
            else (safe_code(state["code"], check=check) or "worker_result_unknown") if outcome == "failed"
            else ("dependency_failed" if check == "publication_correlation" else "workspace_busy") if outcome == "skipped" else "")
    with transaction.atomic():
        heartbeat = AvitoSchedulerHeartbeat.objects.select_for_update().filter(
            pk="avito-status-monitor", state="running", run_id=run_id).first()
        if not heartbeat:
            return
        entries = heartbeat.checks if isinstance(heartbeat.checks, list) else []
        entries = [entry for entry in entries if isinstance(entry, dict) and
                   (entry.get("connection_id"), entry.get("check")) != (connection_id, check)]
        # Bounded worker evidence; fail closed on corruption.
        if len(entries) >= 80:
            return
        entries.append({"connection_id": connection.pk, "account_id": str(connection.external_id),
            "organization_id": connection.channel.organization_id, "channel_id": connection.channel_id,
            "check": check, "outcome": outcome, "code": code,
            "consistency_guard": state.get("consistency_guard")
                if check == "autoload_report" and outcome == "failed" and code == "autoload_report_changed" else None,
            "checked_at": timezone.now().isoformat()})
        heartbeat.checks = entries
        heartbeat.save(update_fields=["checks"])


def _saved(connection, check):
    metadata = connection.settings if isinstance(connection.settings, dict) else {}
    if check == "statuses":
        from .avito_status_monitor import _scope_error
        monitor = AvitoStatusMonitor.objects.select_related("recipient").filter(connection=connection).first()
        if not monitor:
            return {"configured": False, "enabled": False, "scope_valid": None, "code": "not_configured",
                    "checked_at": None, "success_at": None, "next_due_at": None}
        scope_error = _scope_error(monitor, connection)
        valid = not bool(scope_error)
        return {"configured": True, "enabled": monitor.enabled, "scope_valid": valid,
            "code": safe_code(monitor.last_error_code) if valid else safe_code(scope_error),
            "checked_at": max(filter(None, (monitor.last_success_at, monitor.last_failure_at)), default=None),
            "success_at": monitor.last_success_at, "next_due_at": monitor.next_due_at}
    if check == "messages_digest":
        from .avito_deferred_messages import allowed_actor, KEY
        value = metadata.get(KEY)
        state = value if isinstance(value, dict) else {}
        valid = bool(state and allowed_actor(connection, state))
        return {"configured": bool(state), "enabled": bool(state),
            "scope_valid": valid if state else None,
            "code": safe_code(state.get("last_error_code", "")) if valid or not state else "binding_changed",
            "checked_at": None, "success_at": None, "next_due_at": when(state.get("next_due_at"))}
    value = metadata.get(KEYS[check])
    state = value if isinstance(value, dict) else {}
    if check == "advance":
        from .avito_advance import _allowed
    elif check == "statistics_daily":
        from .avito_daily_statistics import _allowed
    elif check in {"autoload_report", "publication_correlation"}:
        from .avito_autoload_auto import _allowed
    else:
        from .avito_tariff_reminder import _allowed
    valid = bool(state and _allowed(connection, state))
    code = safe_code(state.get("last_publication_error_code" if check == "publication_correlation" else "last_error_code", ""), check=check)
    if check == "tariff" and state.get("last_error_detail"):
        code = "tariff_notification_preferences_disabled"
    return {"configured": bool(state), "enabled": state.get("enabled") is True,
        "scope_valid": valid if state else None, "code": code if valid or not state else "binding_changed",
        "consistency_guard": consistency_guard(state.get("last_consistency_guard"))
            if check == "autoload_report" and valid and code == "autoload_report_changed" else None,
        "checked_at": when(state.get("last_publication_checked_at" if check == "publication_correlation" else "last_checked_at")),
        "success_at": when(state.get("last_publication_success_at" if check == "publication_correlation" else "last_success_at")),
        "next_due_at": when(state.get("next_due_at"))}


def checks(connection, *, only_check=None):
    """Auth scope is checked by the caller; rows expose only this exact binding."""
    try:
        heartbeat = AvitoSchedulerHeartbeat.objects.filter(pk="avito-status-monitor").first()
    except DatabaseError:
        heartbeat = None
    complete = bool(heartbeat and heartbeat.state == "finished" and heartbeat.finished_at and
        heartbeat.started_at <= heartbeat.finished_at <= timezone.now())
    recorded = heartbeat.checks if complete and isinstance(heartbeat.checks, list) else []
    output = []
    for key, label in CHECKS.items():
        if only_check is not None and key != only_check:
            continue
        saved = _saved(connection, key)
        match = next((row for row in recorded if isinstance(row, dict) and
            row.get("connection_id") == connection.pk and row.get("account_id") == str(connection.external_id)
            and row.get("organization_id") == connection.channel.organization_id
            and row.get("channel_id") == connection.channel_id and row.get("check") == key
            and row.get("outcome") in OUTCOMES and when(row.get("checked_at"))
            and heartbeat.started_at <= when(row["checked_at"]) <= heartbeat.finished_at), None)
        current_code = saved["code"]
        state = ("disabled" if not saved["enabled"] else "blocked" if saved["scope_valid"] is False
            else "partial" if key == "statistics_daily" and current_code in {"statistics_fields_unavailable", "statistics_total_unavailable"}
            else "failed" if current_code else "pending" if not saved["success_at"] else
            "due" if saved["next_due_at"] and saved["next_due_at"] <= timezone.now() else "ok")
        output.append({"check": key, "label": label, "state": state, **saved,
            "checked_at": saved["checked_at"].isoformat() if saved["checked_at"] else None,
            "success_at": saved["success_at"].isoformat() if saved["success_at"] else None,
            "next_due_at": saved["next_due_at"].isoformat() if saved["next_due_at"] else None,
            "last_worker_outcome": match["outcome"] if match else None,
            "last_worker_code": safe_code(match["code"], check=key) if match else None,
            "last_worker_checked_at": match["checked_at"] if match else None,
            "consistency_guard": saved.get("consistency_guard"),
            "consistency_guard_label": CONSISTENCY_GUARDS.get(saved.get("consistency_guard"), ""),
            "last_worker_consistency_guard": consistency_guard(match.get("consistency_guard"))
                if match and key == "autoload_report" and match["outcome"] == "failed" and match["code"] == "autoload_report_changed" else None,
            "last_worker_consistency_guard_label": CONSISTENCY_GUARDS.get(consistency_guard(match.get("consistency_guard")), "")
                if match and key == "autoload_report" and match["outcome"] == "failed" and match["code"] == "autoload_report_changed" else "",
            "last_worker_verified": bool(match)})
    return output


def scoped_scheduler_display(connection, check, scheduler):
    """Keep activation gates intact; attribute display evidence to one binding/check."""
    from zoneinfo import ZoneInfo
    from .avito_scheduler import MAX_AGE

    if check not in CHECKS:
        raise ValueError("Unknown Avito check")
    output = dict(scheduler)
    label = f"Подключение №{connection.pk} · {CHECKS[check]}"
    row = next(item for item in checks(connection, only_check=check) if item["check"] == check)
    now = timezone.now()
    stamp = when(row.get("last_worker_checked_at"))
    saved_stamp = when(row.get("checked_at"))

    def display_time(value):
        return value.astimezone(ZoneInfo("Asia/Barnaul")).strftime("%d.%m.%Y %H:%M:%S") + " · Барнаул" if value else "время не подтверждено"

    fresh = bool(row.get("last_worker_verified") and stamp and
        timedelta(0) <= now - stamp <= MAX_AGE and
        scheduler.get("code") in {"ready", "partial", "failed"})
    failed = False
    state = "not_confirmed"
    if not row["configured"] or not row["enabled"]:
        state, message = "disabled", "Проверка выключена или ещё не настроена."
    elif row["scope_valid"] is False:
        state, message = "blocked", "Настройки или права подключения требуют повторного подтверждения."
    elif row["state"] == "partial":
        state, message = "partial", "Ответ не содержит достаточных данных: полнота не подтверждена (" + safe_code(row["code"], check=check) + ")."
    elif fresh and row["last_worker_outcome"] == "failed":
        failed, state = True, "failed"
        message = "Проверка завершилась ошибкой (" + safe_code(row["last_worker_code"], check=check) + ")."
    elif fresh and row["last_worker_outcome"] in {"baseline", "success"}:
        state, message = "success", "Успешная проверка подтверждена."
    elif fresh and row["last_worker_outcome"] in {"skipped", "not_run"}:
        state, message = "skipped", "В этом запуске проверка не выполнялась; сбой не подтверждён."
    elif row["state"] == "due":
        state, message = "due", "Проверка ожидает запуска по расписанию; нового результата ещё нет."
    else:
        message = "Нет свежего подтверждённого результата этой проверки."

    # No global partial/exit status or another account's row becomes this check's error.
    result_time = display_time(stamp) if fresh and state not in {"disabled", "blocked"} else "время нового результата не подтверждено"
    output.update(detail=f"{label} · {result_time}: {message}",
        display_failed=failed, display_state=state, scope_label=label,
        checked_at_display=display_time(saved_stamp))
    return output
