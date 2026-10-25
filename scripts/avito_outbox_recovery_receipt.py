"""Safe projection for the one approved original reply; no customer payload."""
import json
import sys
try:
    from scripts.avito_outbox_receipt import require, integer, timestamp, safe_code
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.avito_outbox_receipt"}:
        raise
    from avito_outbox_receipt import require, integer, timestamp, safe_code

FRAME = "AVITO_OUTBOX_ORIGINAL_RECEIPT "
MAX_BYTES = 8192
ERRORS = {"", "scope_invalid", "original_target_not_unique", "original_target_mismatch",
    "original_target_changed", "original_recovery_unknown", "original_recovery_unconfirmed",
    "original_provider_match_changed"}
STATUSES = {"pending", "sending", "delivered", "failed", "received"}


def normalize(value):
    require(isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 1)
    require(value.get("source") == "services_original_reply_recovery_v1")
    require(type(value.get("connection_id")) is int and value["connection_id"] == 4 and value.get("account_id") == "446312402")
    require(value.get("duplicate_created") is False and value.get("client_read_confirmed") is False)
    require(value.get("outcome") in {"held", "accepted", "pending", "failed", "unknown"})
    require(value.get("provider_before_state") in {"not_checked", "accepted", "absent", "unknown"})
    code = value.get("error_code")
    if code not in ERRORS:
        code = safe_code(code)
    result = {"schema_version": 1, "source": value["source"], "checked_at": timestamp(value.get("checked_at")),
        "connection_id": 4, "account_id": "446312402", "outcome": value["outcome"], "error_code": code,
        "candidate_count": integer(value.get("candidate_count"), 2), "local_id": value.get("local_id"),
        "send_function_invocations": integer(value.get("send_function_invocations"), 1),
        "provider_before_state": value["provider_before_state"], "duplicate_created": False, "client_read_confirmed": False,
        "initial_error_code": safe_code(value.get("initial_error_code"))}
    if result["local_id"] is not None:
        require(integer(result["local_id"]) > 0 and result["candidate_count"] == 1)
    for key in ("tuple_matches", "provider_before_complete", "provider_message_id_present", "ledger_reused",
                "initial_has_saved_error", "initial_external_id_present"):
        require(type(value.get(key)) is bool)
        result[key] = value[key]
    for prefix in ("initial", "final"):
        state, attempts = value.get(prefix + "_status"), value.get(prefix + "_attempts")
        require(state is None or state in STATUSES)
        require(attempts is None or type(attempts) is int and 0 <= attempts <= 65535)
        result[prefix + "_status"], result[prefix + "_attempts"] = state, attempts
    require(result["initial_has_saved_error"] == bool(result["initial_error_code"]))
    if result["send_function_invocations"]:
        require(result["tuple_matches"] and result["provider_before_state"] == "absent" and result["provider_before_complete"])
        require(result["initial_status"] == "pending" and result["initial_attempts"] == 0)
    if result["outcome"] == "accepted":
        require(result["tuple_matches"] and result["provider_message_id_present"] and result["final_status"] == "delivered")
    return result


def main(argv=None):
    try:
        with open((argv or sys.argv[1:])[0], "rb") as file:
            raw = file.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES)
        frames = [line[len(FRAME):] for line in raw.decode().splitlines() if line.startswith(FRAME)]
        require(len(frames) == 1)
        result = normalize(json.loads(frames[0]))
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError):
        print("AVITO_OUTBOX_ORIGINAL_UNAVAILABLE receipt_invalid")
        return 2
    print("AVITO_OUTBOX_ORIGINAL_RESULT " + json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
