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
