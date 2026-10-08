"""Read-only evidence of a successful bounded worker tick, never a cron installer."""
import json
import os
from pathlib import Path
import stat
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

MAX_AGE = timedelta(minutes=45)
MESSAGES = {
    "unverified": "Расписание сервера ещё не подтверждено. Настройте задачу в панели хостинга и проверьте её выполнение.",
    "running": "Серверный обработчик выполняется; успешный результат ещё не подтверждён.",
    "failed": "Последний запуск обработчика завершился ошибкой. Проверьте состояние подключения и расписание.",
    "stale": "Нет свежего успешного запуска обработчика. Проверьте расписание на хостинге.",
    "ready": "Успешный запуск серверного обработчика подтверждён.",
}


def _moment(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    result = datetime.fromisoformat(value)
    if timezone.is_naive(result):
        raise ValueError
    return result


def scheduler_state():
    state, finished = "unverified", None
    path = Path(settings.BASE_DIR).parent / "tmp/service2-avito-cron/last-tick.json"
    try:
        directory = path.parent.lstat()
        if not stat.S_ISDIR(directory.st_mode) or directory.st_uid != os.getuid() or directory.st_mode & 0o077:
            raise ValueError
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError
            raw = stream.read(1025)
        if len(raw) > 1024:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        started = _moment(data.get("started_at"))
        now = timezone.now()
        if started > now or now - started > MAX_AGE:
            state = "stale"
        elif data.get("state") == "running":
            state = "running"
        elif data.get("state") == "finished" and type(data.get("exit_code")) is int:
            finished = _moment(data.get("finished_at"))
            if not started <= finished <= now:
                raise ValueError
            state = "ready" if data["exit_code"] == 0 and now - finished <= MAX_AGE else "failed"
    except (OSError, ValueError, TypeError):
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
