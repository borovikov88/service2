#!/usr/bin/env python3
"""Allowlist aggregate proof only; never echo SSH output or private failure text."""
import datetime as dt
import json
from pathlib import Path
import re
import sys

PREFIX = "CRM_PILOT_DB_BACKUP_V1 "
FIELDS = {"kind", "schema_version", "expected_sha", "table_count", "size_bytes", "uncompressed_bytes",
          "sha256", "started_at", "completed_at", "integrity_verified", "restore_verified",
          "schema_sha256", "backup_file"}
CAPTURE_LIMIT = 16384
# Independent closed vocabulary: do not import settings or run the backup helper.
HELPER_PHASES = frozenset(("initial", "lock", "checkout", "native_tool", "settings", "client_options",
                         "private_directory", "schema_before", "metadata_lock", "schema_locked",
                         "options_file", "dump_stream", "schema_after", "gzip_verify", "checkout_after", "publish"))
HELPER_ERRORS = frozenset(("guard_failed", "unexpected", "native_failed", "subprocess_timeout",
                         "subprocess_failed", "import_failed", "os_error", "database_error"))
HELPER_REASONS = frozenset(("unspecified", "lock_directory_path", "lock_directory_owner_mode",
                          "lock_file_owner_type", "lock_file_mode", "lock_file_replaced",
                          "checkout_head", "checkout_path", "checkout_dirty", "checkout_untracked",
                          "native_missing", "native_version_size", "native_version_unsupported",
                          "native_help_size", "private_path", "private_parent_type", "private_parent_mode",
                          "private_owner_type", "private_mode", "client_engine", "client_database",
                          "client_credentials", "client_options", "client_mirror", "client_port", "app_path",
                          "database_timeout", "heartbeat_session", "heartbeat_autocommit", "heartbeat_wait_timeout",
                          "native_parent_guard", "native_process_threads"))
REMOTE_ERRORS = {"arguments": {"invalid_count", "invalid_sha", "invalid_path"},
                 "path": {"not_directory", "change_failed", "noncanonical_checkout"},
                 "git": {"toplevel_failed", "toplevel_mismatch", "diff_failed"},
                 "head": {"read_failed", "mismatch"},
                 "origin": {"read_failed", "invalid"},
                 "main": {"lookup_failed", "mismatch"},
                 "runtime": {"layout", "timeout_missing", "helper_missing"},
                 "exec": {"command_failed"}}
HELPER_PREFIX = b"CRM_PILOT_DB_BACKUP_BLOCKED "
REMOTE_PREFIX = b"CRM_PILOT_DB_BACKUP_REMOTE_BLOCKED "


def failure(stdout, stderr, ssh_exit):
    """Return reconstructed fixed tokens only; never reuse an unvalidated line."""
    if not re.fullmatch(r"[1-9][0-9]{0,2}", ssh_exit) or not 1 <= int(ssh_exit) <= 255:
        ssh_exit = "0"  # Invalid input is not a successful SSH result.
        status = "invalid_exit"
    else:
        status = "absent"
    base = "CRM_PILOT_DB_BACKUP_TRANSPORT_BLOCKED ssh_exit=" + ssh_exit
    if status == "invalid_exit":
        return base + " diagnostic=" + status
    if len(stdout) > CAPTURE_LIMIT or len(stderr) > CAPTURE_LIMIT:
        return base + " diagnostic=oversized"
    # A nonzero SSH result cannot use even a valid success receipt as proof.
    if any(line.startswith(PREFIX.encode()) for line in stdout.splitlines() + stderr.splitlines()):
        return base + " diagnostic=conflicting_success"
    candidates = [(source, line) for source, data in (("stdout", stdout), ("stderr", stderr))
                  for line in data.splitlines()
                  if line.startswith((b"CRM_PILOT_DB_BACKUP_BLOCKED", b"CRM_PILOT_DB_BACKUP_REMOTE_BLOCKED"))]
    if not candidates:
        return base + " diagnostic=absent"
    if len(candidates) != 1 or candidates[0][0] != "stderr":
        return base + " diagnostic=ambiguous"
    line = candidates[0][1]
    helper = re.fullmatch(HELPER_PREFIX + rb"phase=([a-z_]+) error=([a-z_]+) native_exit=(-?[0-9]{1,3})"
                          rb" reason=([a-z_]+) os_errno=([0-9]{1,3}) database_code=([0-9]{1,4})", line)
    if helper:
        phase, error, code, reason, os_errno, database_code = [item.decode("ascii") for item in helper.groups()]
        numbers = (code, os_errno, database_code)
        canonical = all(str(int(number)) == number for number in numbers)
        if (phase in HELPER_PHASES and error in HELPER_ERRORS and reason in HELPER_REASONS and canonical
                and -127 <= int(code) <= 255 and 0 <= int(os_errno) <= 133
                and (int(database_code) == 0 or 1000 <= int(database_code) <= 2999)):
            return ("CRM_PILOT_DB_BACKUP_FAILURE_V1 source=helper ssh_exit=" + ssh_exit + " phase=" + phase +
                    " error=" + error + " native_exit=" + str(int(code)) + " reason=" + reason +
                    " os_errno=" + str(int(os_errno)) + " database_code=" + str(int(database_code)))
    remote = re.fullmatch(REMOTE_PREFIX + rb"phase=([a-z_]+) error=([a-z_]+)", line)
    if remote:
        phase, error = [item.decode("ascii") for item in remote.groups()]
        if phase in REMOTE_ERRORS and error in REMOTE_ERRORS[phase]:
            return ("CRM_PILOT_DB_BACKUP_FAILURE_V1 source=remote ssh_exit=" + ssh_exit +
                    " phase=" + phase + " error=" + error)
    return base + " diagnostic=malformed"


def failure_main(args):
    try:
        if len(args) != 3:
            raise ValueError()
        captures = []
        for path in args[:2]:
            with Path(path).open("rb") as stream:
                captures.append(stream.read(CAPTURE_LIMIT + 1))
        print(failure(*captures, args[2]))
    except (OSError, ValueError, TypeError):
        print("CRM_PILOT_DB_BACKUP_TRANSPORT_BLOCKED ssh_exit=0 diagnostic=capture_unreadable")
    return 2


def normalize(text, expected):
    if len(text) > 16384 or not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise ValueError()
    lines = [line for line in text.splitlines() if line.startswith(PREFIX)]
    if len(lines) != 1:
        raise ValueError()
    value = json.loads(lines[0][len(PREFIX):])
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError()
    if value["kind"] != "crm_pilot_db_backup" or type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError()
    if value["expected_sha"] != expected or value["integrity_verified"] is not True or value["restore_verified"] is not False:
        raise ValueError()
    for name, limit in (("table_count", 2000), ("size_bytes", 512 * 1024 ** 2), ("uncompressed_bytes", 1024 ** 3)):
        if type(value[name]) is not int or not 0 < value[name] <= limit:
            raise ValueError()
    for name in ("sha256", "schema_sha256"):
        if not isinstance(value[name], str) or not re.fullmatch(r"[0-9a-f]{64}", value[name]):
            raise ValueError()
    if not isinstance(value["backup_file"], str) or not re.fullmatch(expected + r"-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}\.sql\.gz", value["backup_file"]):
        raise ValueError()
    dates = []
    for name in ("started_at", "completed_at"):
        if not isinstance(value[name], str) or len(value[name]) > 40:
            raise ValueError()
        date = dt.datetime.fromisoformat(value[name])
        if date.tzinfo is None or date.utcoffset() != dt.timedelta(0):
            raise ValueError()
        dates.append(date)
    if not 0 <= (dates[1] - dates[0]).total_seconds() <= 300:
        raise ValueError()
    return value


def main():
    if sys.argv[1:2] == ["--failure"]:
        return failure_main(sys.argv[2:])
    try:
        with Path(sys.argv[1]).open() as stream:
            text = stream.read(16385)
        value = normalize(text, sys.argv[2])
        print(PREFIX + json.dumps(value, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        print("CRM_PILOT_DB_BACKUP_RECEIPT_BLOCKED")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
