"""Read-only evidence of a successful bounded worker tick, never a cron installer."""
from pathlib import Path
from datetime import timedelta

from django.conf import settings
from django.db import DatabaseError
from django.utils import timezone
from pool_service.communication_models import AvitoSchedulerHeartbeat

HEARTBEAT_KEY = "avito-status-monitor"
MAX_AGE = timedelta(minutes=45)
MESSAGES = {
    "unverified": "Расписание сервера ещё не подтверждено. Настройте задачу в панели хостинга и проверьте её выполнение.",
    "running": "Серверный обработчик выполняется; успешный результат ещё не подтверждён.",
    "failed": "Последний запуск обработчика завершился ошибкой. Проверьте состояние подключения и расписание.",
    "stale": "Нет свежего успешного запуска обработчика. Проверьте расписание на хостинге.",
    "ready": "Успешный запуск серверного обработчика подтверждён.",
}


def scheduler_state():
    # Cron and Passenger may have different UIDs/private filesystem views.
    # Only the supervisor writes this shared row; page reads never create it.
    state, finished = "unverified", None
    try:
        record = AvitoSchedulerHeartbeat.objects.filter(pk=HEARTBEAT_KEY).first()
        if record is not None:
            started, now = record.started_at, timezone.now()
            if timezone.is_naive(started):
                raise ValueError
            if started > now or now - started > MAX_AGE:
                state = "stale"
            elif record.state == "running":
                state = "running"
            elif record.state == "finished" and type(record.exit_code) is int:
                finished = record.finished_at
                if finished is None or timezone.is_naive(finished) or not started <= finished <= now:
                    raise ValueError
                state = "ready" if record.exit_code == 0 and now - finished <= MAX_AGE else "failed"
    except (DatabaseError, ValueError, TypeError):
        # Missing migrations / DB failure must not grant activation or leak SQL.
        state, finished = "unverified", None
    return {"ready": state == "ready", "code": state, "detail": MESSAGES[state],
            "success_at": finished if state == "ready" else None}


def panel_command():
    """Exact deployed paths; callers must first verify the subscriber's scope.

    This only formats the existing tick invocation. It reads no crontab, secrets,
    or provider data and does not create a directory, schedule, or readiness proof.
    """
    try:
        from scripts.avito_monitor_cron import CronManager, CronError
    except ImportError:
        return ""
    try:
        return CronManager(str(Path(settings.BASE_DIR).resolve())).command()
    except (OSError, ValueError, CronError):
        return ""
