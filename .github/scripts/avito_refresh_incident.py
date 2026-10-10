"""Fixed, read-only summary of the 2026-10-08 Avito refresh incident.

Only standard-library code runs on the host. No Django settings are loaded.
Never print log messages, exception values, source lines or absolute paths.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat


MARKER = "AVITO_REFRESH_INCIDENT="
MAX_BYTES = 1024 * 1024
START = datetime(2026, 10, 8, 13, 8, tzinfo=timezone.utc)
END = datetime(2026, 10, 8, 13, 11, tzinfo=timezone.utc)
# Django sets the process timezone from service_site/settings.py: TIME_ZONE='UTC'.
# Standard logging timestamps omit the offset; naive timestamps mean UTC here.
HEADER = re.compile(
    r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d{1,6})?(?:Z|[+-]\d{2}:\d{2})?) "
    r"ERROR django\.request status=500 Internal Server Error: /avito/[0-9]{1,12}/refresh/$"
)
ANY_HEADER = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")
FRAME = re.compile(r'^  File "[^"\r\n]+/((?:pool_service|service_site)/[a-z_]+\.py)", line ([0-9]{1,6}), in [A-Za-z_][A-Za-z_0-9]*$')
FILES = frozenset({
    "pool_service/avito_management.py", "pool_service/avito_workspace.py",
    "pool_service/communication_avito.py", "pool_service/communication_models.py",
    "pool_service/communication_security.py", "pool_service/middleware.py",
    "service_site/logging_handlers.py",
})
EXCEPTIONS = frozenset({
    "TypeError", "ValueError", "AttributeError", "KeyError", "IndexError",
    "OverflowError", "RecursionError", "UnicodeEncodeError", "UnicodeDecodeError",
    "RuntimeError", "TimeoutError", "OSError", "MemoryError",
    "django.db.utils.OperationalError", "django.db.utils.InterfaceError",
    "django.db.utils.IntegrityError", "django.db.utils.DatabaseError",
    "django.db.utils.DataError", "django.db.utils.ProgrammingError",
    "django.db.transaction.TransactionManagementError",
    "MySQLdb.OperationalError", "MySQLdb.InterfaceError", "MySQLdb.DataError",
    "MySQLdb.IntegrityError", "MySQLdb.ProgrammingError",
})
EXCEPTION_LINE = re.compile(r"^([A-Za-z_][A-Za-z_0-9.]*(?:Error|Exception))(?::|$)")


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace(",", ".").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def public_summary(value):
    """Validate output again on the runner before any remote stdout is printed."""
    if (not isinstance(value, dict) or not isinstance(value.get("status"), str)
            or value["status"] not in {"ok", "log_unavailable", "log_not_regular"}):
        raise ValueError("Invalid incident summary")
    records = value.get("records")
    if not isinstance(records, list) or len(records) > 10:
        raise ValueError("Invalid incident records")
    clean = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Invalid incident record")
        when = _timestamp(record.get("timestamp", ""))
        exceptions, frames = record.get("exceptions"), record.get("frames")
        if not when or not START <= when < END or not isinstance(exceptions, list) or not isinstance(frames, list):
            raise ValueError("Invalid incident fields")
        if len(exceptions) > 6 or any(not isinstance(x, str) or x not in EXCEPTIONS for x in exceptions):
            raise ValueError("Invalid incident exception")
        safe_frames = []
        if len(frames) > 12:
            raise ValueError("Invalid incident frames")
        for frame in frames:
            if (not isinstance(frame, dict) or not isinstance(frame.get("file"), str)
                    or frame["file"] not in FILES or type(frame.get("line")) is not int
                    or not 1 <= frame["line"] <= 999999):
                raise ValueError("Invalid incident frame")
            safe_frames.append({"file": frame["file"], "line": frame["line"]})
        clean.append({"timestamp": when.isoformat(), "exceptions": exceptions,
                      "frames": safe_frames, "unknown_exception_withheld": record.get("unknown_exception_withheld") is True})
    return {"status": value["status"], "source_timezone": "UTC", "window_start": START.isoformat(),
            "window_end_exclusive": END.isoformat(), "tail_cutoff": value.get("tail_cutoff") is True,
            "records": clean}


def summarize(raw, *, tail_cutoff=False):
    text = raw.decode("utf-8", errors="replace")
    # Never treat an incomplete first line from the bounded tail as a new record.
    if tail_cutoff:
        text = text.partition("\n")[2]
    records, current = [], None
    for line in text.splitlines():
        if ANY_HEADER.match(line):
            current = None
            match = HEADER.fullmatch(line)
            when = _timestamp(match[1]) if match else None
            if when and START <= when < END:
                current = {"timestamp": when.isoformat(), "exceptions": [], "frames": [],
                           "unknown_exception_withheld": False}
                records.append(current)
                records = records[-10:]
            continue
        if current is None:
            continue
        frame = FRAME.fullmatch(line)
        if frame and frame[1] in FILES:
            entry = {"file": frame[1], "line": int(frame[2])}
            if entry["line"] and entry not in current["frames"]:
                current["frames"] = (current["frames"] + [entry])[-12:]
        exception = EXCEPTION_LINE.match(line)
        if exception:
            name = exception[1]
            if name in EXCEPTIONS:
                if name not in current["exceptions"]:
                    current["exceptions"] = (current["exceptions"] + [name])[-6:]
            else:
                current["unknown_exception_withheld"] = True
    return public_summary({"status": "ok", "tail_cutoff": tail_cutoff, "records": records})


def read_fixed_log():
    # The existing SSH runner has already entered its configured app checkout.
    # No caller-supplied path or command is accepted.
    log = Path.cwd().parent / "var" / "log" / "django-request-error.log"
    try:
        if any(path.is_symlink() for path in (log.parent.parent, log.parent, log)):
            return public_summary({"status": "log_not_regular", "records": []})
        descriptor = os.open(log, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return public_summary({"status": "log_not_regular", "records": []})
            cutoff = info.st_size > MAX_BYTES
            handle.seek(max(0, info.st_size - MAX_BYTES))
            return summarize(handle.read(MAX_BYTES), tail_cutoff=cutoff)
    except OSError:
        return public_summary({"status": "log_unavailable", "records": []})


if __name__ == "__main__":
    print(MARKER + json.dumps(read_fixed_log(), ensure_ascii=True, separators=(",", ":")))
