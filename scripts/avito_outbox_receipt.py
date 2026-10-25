"""Strict public projection of private, read-only Services outbox evidence."""
import json
import re
import sys
from datetime import datetime, timezone

try:
    from scripts.avito_readonly_receipt import webhook_chain
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.avito_readonly_receipt"}:
        raise
    from avito_readonly_receipt import webhook_chain

FRAME = "AVITO_OUTBOX_READONLY_RECEIPT "
RESULT_FRAME = "AVITO_OUTBOX_READONLY_RESULT "
MAX_BYTES = 32768
ERROR_CODES = {
    "", "provider_unavailable", "provider_account_mismatch", "provider_credentials_missing",
    "provider_credentials_invalid", "provider_token_missing", "provider_invalid_json",
    "provider_invalid_response", "provider_response_too_large", "provider_reconcile_incomplete",
    "provider_reconcile_invalid", "provider_reconcile_ambiguous", "scope_invalid",
    "legacy_error_unclassified", "audit_provider_unknown", "audit_binding_changed",
    "provider_delivery_unknown", "attachments_require_provider_upload", "empty_message",
    "outbox_scope_invalid", "outbox_sender_access_revoked", "outbox_internal_error", "outbox_legacy_held",
    "outbox_transaction_open", "outbox_binding_changed",
    "provider_send_unknown", "delivery_unknown", "worker_unavailable",
}
WORKER_STATES = {"not_configured", "disabled", "ready", "healthy", "running", "idle", "ok",
    "stale", "overdue", "failed", "error", "unknown", "binding_changed", "unavailable", "never_run"}


def require(condition):
    if not condition:
        raise ValueError("invalid outbox receipt")


def integer(value, limit=10**10):
    require(type(value) is int and 0 <= value <= limit)
    return value


def timestamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    require(isinstance(value, str) and len(value) <= 40)
    parsed = datetime.fromisoformat(value)
    require(parsed.utcoffset() is not None)
    return value


def safe_code(value):
    require(isinstance(value, str) and (value in ERROR_CODES or re.fullmatch(r"provider_http_[1-5][0-9]{2}", value)))
    return value


def worker(value):
    require(isinstance(value, dict) and value.get("worker_state") in WORKER_STATES)
    result = {"worker_state": value["worker_state"]}
    for key in ("last_started_at", "last_success_at", "oldest_pending_at"):
        result[key] = timestamp(value.get(key), nullable=True)
    result["last_error_code"] = safe_code(value.get("last_error_code", ""))
    for key in ("pending_count", "unknown_count", "failed_count", "legacy_held_count"):
        result[key] = integer(value.get(key), 10**7)
    age = value.get("oldest_pending_age_seconds")
    result["oldest_pending_age_seconds"] = integer(age, 10**10) if age is not None else None
    require((result["pending_count"] > 0) == (result["oldest_pending_at"] is not None))
    require((result["oldest_pending_at"] is not None) == (age is not None))
    return result


def scheduler(value):
    require(isinstance(value, dict) and value.get("source") == "hosting_user_crontab_cli")
    state = value.get("state")
    require(state in {"unavailable", "failed", "checked"} and value.get("panel_jobs_verified") is False)
    result = {"source": "hosting_user_crontab_cli", "state": state, "panel_jobs_verified": False}
    for key in ("services_outbox_entry_present", "other_outbox_entry_present", "account_monitor_entry_present"):
        field = value.get(key)
        require(type(field) is bool if state == "checked" else field is None)
        result[key] = field
    return result


def message(value):
    require(isinstance(value, dict) and value.get("status") in {"pending", "sending", "delivered", "failed", "received"})
    require(value.get("post_evidence") in {"accepted_local", "attempted_or_unknown", "not_claimed", "not_recorded"})
    result = {"local_id": integer(value.get("local_id")), "created_at": timestamp(value.get("created_at")),
        "status": value["status"], "attempts": integer(value.get("attempts"), 65535),
        "error_code": safe_code(value.get("error_code")), "accepted_at": timestamp(value.get("accepted_at"), nullable=True),
        "post_evidence": value["post_evidence"]}
    require(result["local_id"] > 0)
    for key in ("has_saved_error", "external_id_present", "sent_by_present", "requested_tuple_matches"):
        require(type(value.get(key)) is bool)
        result[key] = value[key]
    digest = value.get("body_sha256")
    require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest))
    result["body_sha256"] = digest
    if result["post_evidence"] == "not_claimed":
        require(result["status"] == "pending" and result["attempts"] == 0 and not result["external_id_present"])
    if result["post_evidence"] == "accepted_local":
        require(result["status"] == "delivered" and result["external_id_present"])
    return result


def reconciliation(value):
    require(isinstance(value, dict) and value.get("state") in {"not_checked", "accepted", "absent", "unknown"})
    result = {"state": value["state"], "error_code": safe_code(value.get("error_code")),
        "pages": integer(value.get("pages"), 100), "messages_checked": integer(value.get("messages_checked"), 10000)}
    for key in ("complete", "provider_id_present"):
        require(type(value.get(key)) is bool)
        result[key] = value[key]
    if result["state"] == "accepted":
        require(result["provider_id_present"])
    if result["state"] == "absent":
        require(result["complete"] and not result["provider_id_present"] and result["pages"] > 0)
    if result["state"] == "not_checked":
        require(not result["complete"] and not result["provider_id_present"] and result["pages"] == result["messages_checked"] == 0)
    return result


def normalize(value):
    require(isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 1)
    require(value.get("source") == "services_outbox_incident_readonly")
    require(type(value.get("connection_id")) is int and value["connection_id"] == 4 and value.get("account_id") == "446312402")
    require(value.get("customer_post_performed") is False and value.get("queue_rows_changed") is False)
    start, end = timestamp(value.get("incident_start")), timestamp(value.get("incident_end"))
    require(datetime.fromisoformat(start).astimezone(timezone.utc).isoformat() == "2026-10-25T01:27:00+00:00")
    require(datetime.fromisoformat(end).astimezone(timezone.utc).isoformat() == "2026-10-25T01:28:00+00:00")
    count = integer(value.get("candidate_count"), 2)
    row = message(value.get("message")) if count == 1 else None
    require(row is not None or value.get("message") is None)
    proof = reconciliation(value.get("reconciliation"))
    require(count == 1 or proof["state"] == "not_checked")
    return {"schema_version": 1, "source": value["source"], "checked_at": timestamp(value.get("checked_at")),
        "connection_id": 4, "account_id": "446312402", "incident_start": start, "incident_end": end,
        "candidate_count": count, "message": row, "reconciliation": proof,
        "scheduler": scheduler(value.get("scheduler")), "worker": worker(value.get("worker")),
        "webhook_chain": webhook_chain(value.get("webhook_chain")),
        "customer_post_performed": False, "queue_rows_changed": False}


def main(argv=None):
    try:
        with open((argv or sys.argv[1:])[0], "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES)
        frames = [line[len(FRAME):] for line in raw.decode("utf-8").splitlines() if line.startswith(FRAME)]
        require(len(frames) == 1)
        result = normalize(json.loads(frames[0]))
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        print("AVITO_OUTBOX_READONLY_UNAVAILABLE receipt_invalid")
        return 2
    print(RESULT_FRAME + json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
