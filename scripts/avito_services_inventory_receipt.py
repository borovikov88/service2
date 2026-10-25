"""Closed database-only Services photo-inventory projection; no Django."""
import json
import sys
from datetime import datetime

MAX_BYTES = 16384
RESULT = "AVITO_SERVICES_LOCAL_INVENTORY_RESULT "
UNAVAILABLE = "AVITO_SERVICES_LOCAL_INVENTORY_UNAVAILABLE receipt_invalid"
CODES = {"", "file_check_scope_invalid", "file_check_external_reserved", "file_check_context_changed",
    "file_check_needs_owner_original", "file_check_needs_second_photo", "file_check_busy",
    "file_check_failed", "file_check_completed", "file_check_result_unknown", "file_check_release_denied"}
COUNTS = ("asset_count", "draft_referenced_asset_count", "approved_shared_asset_count", "cover_order_candidate_count")


def require(value):
    if not value:
        raise ValueError("invalid local inventory")


def number(value, limit=100000):
    require(type(value) is int and 0 <= value <= limit)
    return value


def normalize(value):
    require(isinstance(value, dict))
    fixed = {"schema_version": 1, "kind": "avito_services_local_file_inventory",
        "connection_id": 4, "account_id": "446312402",
        "source_kind": "stored_normalized_jpeg", "raw_original_retained": False,
        "browser_clicks_verified": False, "external_http_checked": False, "external_generation_required": True}
    for key, expected in fixed.items():
        require(type(value.get(key)) is type(expected) and value[key] == expected)
    result = dict(fixed)
    stamp = value.get("checked_at")
    require(isinstance(stamp, str) and len(stamp) <= 40 and datetime.fromisoformat(stamp).utcoffset() is not None)
    result["checked_at"] = stamp
    require(isinstance(value.get("state"), str) and value["state"] in {"blocked", "ready", "completed"}
        and isinstance(value.get("code"), str) and value["code"] in CODES)
    require(isinstance(value.get("operation_state"), str)
        and value["operation_state"] in {"not_started", "invalid", "pending", "completed"})
    result.update(state=value["state"], code=value["code"], operation_state=value["operation_state"])
    missing = [value.get(key) is None for key in COUNTS]
    require(all(missing) or not any(missing))
    known = not any(missing)
    for key in COUNTS:
        result[key] = number(value.get(key)) if known else None
    match = value.get("draft_matches_published")
    require(type(match) is bool if known else match is None)
    result["draft_matches_published"] = match
    for key in ("draft_revision", "published_revision"):
        entry = value.get(key)
        require(type(entry) is int and 0 <= entry < 2**63 if known else entry is None)
        result[key] = entry
    if known:
        require(result["draft_revision"] > 0)
        require(result["approved_shared_asset_count"] <= result["draft_referenced_asset_count"] <= result["asset_count"])
        require(result["cover_order_candidate_count"] <= 2000)
        require(not result["cover_order_candidate_count"] or result["approved_shared_asset_count"] > 0)
    if result["state"] == "ready":
        require(known and result["code"] == "" and result["operation_state"] == "not_started")
        require(result["approved_shared_asset_count"] > 0 and result["cover_order_candidate_count"] > 0)
    elif result["state"] == "completed":
        require(known and result["code"] == "file_check_completed" and result["operation_state"] == "completed")
    else:
        require(result["code"] != "")
    if result["code"] == "file_check_needs_owner_original":
        require(known and result["approved_shared_asset_count"] == 0)
    if result["code"] == "file_check_needs_second_photo":
        require(known and result["approved_shared_asset_count"] > 0 and result["cover_order_candidate_count"] == 0)
    return result


def main(argv=None):
    try:
        with open((argv or sys.argv[1:])[0], "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES)
        result = normalize(json.loads(raw.decode("utf-8")))
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        print(UNAVAILABLE)
        return 2
    print(RESULT + json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
