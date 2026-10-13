"""Schema projection for private, bounded Avito audit receipts; no Django."""
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

FRAME = "AVITO_READONLY_AUDIT_RECEIPT "
MAX_BYTES = 262144
STATES = {"active", "removed", "old", "blocked", "rejected"}
CODE_PATTERN = re.compile(r"(?:provider_http_[1-5][0-9]{2}|audit_[a-z_]{1,60}|autoload_report_[a-z_]{1,60}|source_[a-z_]{1,60}|provider_[a-z_]{1,60})\Z")

def require(value):
    if not value:
        raise ValueError("invalid receipt")

def integer(value, limit=10000000000):
    require(type(value) is int and 0 <= value <= limit)
    return value

def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,32}", value))
    return value

def xml_id(value):
    if value is None:
        return None
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}_[0-9]{1,10}(?:_[0-9]{1,10})?(?:_option_[0-9]{1,10}_[0-9]{1,10})?", value))
    return value

def code(value):
    require(isinstance(value, str) and CODE_PATTERN.fullmatch(value))
    return value

def safe_text(value, limit):
    require(isinstance(value, str) and len(value) <= 50000)
    value = " ".join(value.split())
    if re.search(r"(?i)\b(access_token|client_secret|refresh_token|password|authorization)\b", value):
        return "[текст с учётными данными скрыт]"
    value = re.sub(r"https?://\S+|[\w.+%-]+@[\w.-]+\.[A-Za-z]{2,}", "[контакт скрыт]", value, flags=re.I)
    value = re.sub(r"\+?[78](?:[\s()-]*[0-9]){10}(?![0-9])", "[телефон скрыт]", value)
    value = re.sub(r"(?i)\bBearer\s+\S+", "[токен скрыт]", value)
    return value[:limit]

def price(value):
    if value is None:
        return None
    require(isinstance(value, str) and len(value) <= 24)
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid receipt") from None
    require(number.is_finite() and 0 <= number <= 10**12)
    return format(number, "f")

def sequence(value, callback, limit):
    require(isinstance(value, list) and len(value) <= limit)
    return [callback(item) for item in value]

def feed_item(value):
    require(isinstance(value, dict))
    require(value.get("availability") in (None, "В наличии", "Под заказ"))
    return {"xml_id": xml_id(value.get("xml_id")), "price": price(value.get("price")),
            "availability": value.get("availability")}

def issue(value):
    require(isinstance(value, dict) and value.get("type") in {"error", "warning", "alarm", "info"})
    return {"type": value["type"], "code": integer(value.get("code"), 10**12),
            "title": safe_text(value.get("title"), 200), "description": safe_text(value.get("description"), 700)}

def item(value):
    require(isinstance(value, dict))
    result = {"item_id": identifier(value.get("item_id"))}
    if "error" in value:
        result["error"] = code(value["error"])
        return result
    require(value.get("status") in STATES)
    require(value.get("link_state") in {"exact_xml_match", "xml_id_ambiguous", "not_in_current_xml", "no_autoload_id", "xml_unavailable"})
    result.update(status=value["status"], autoload_xml_id=xml_id(value.get("autoload_xml_id")),
                  link_state=value["link_state"],
                  xml_matches=sequence(value.get("xml_matches"), feed_item, 10),
                  report_xml_ids=sequence(value.get("report_xml_ids"), xml_id, 10))
    return result

def report(value):
    require(isinstance(value, dict))
    if "error" in value:
        return {"error": code(value["error"])}
    require(value.get("complete") is True and type(value.get("finished")) is bool)
    require(value.get("upload_status") in ("processing", "success", "success_warning", "error", ""))
    result = {"upload_id": identifier(value.get("upload_id")), "upload_status": value["upload_status"],
              "complete": True, "finished": value["finished"], "total": integer(value.get("total"), 5000)}
    result["counts"] = {key: integer(value.get("counts", {}).get(key, 0), 5000) for key in ("error", "warning", "alarm", "info")}
    require(all(count <= result["total"] for count in result["counts"].values()))
    result["critical_samples"] = sequence(value.get("critical_samples"), lambda row: {
        "xml_id": xml_id(row.get("xml_id")), "avito_id": identifier(row["avito_id"]) if row.get("avito_id") is not None else None,
        "messages": sequence(row.get("messages"), issue, 3)}, 25)
    return result

def connection(value):
    require(isinstance(value, dict))
    result = {"connection_id": integer(value.get("connection_id"))}
    if "error" in value:
        return result | {"error": code(value["error"])}
    result.update(account_id=identifier(value.get("account_id")),
                  items=sequence(value.get("items"), item, 10), autoload_report=report(value.get("autoload_report")))
    return result

def normalize(value):
    require(isinstance(value, dict) and value.get("schema_version") == 1)
    require(value.get("status") in ("read", "partial", "blocked"))
    timestamp = value.get("checked_at")
    require(isinstance(timestamp, str) and len(timestamp) <= 40)
    require(datetime.fromisoformat(timestamp).utcoffset() == timedelta(hours=7))
    result = {"schema_version": 1, "status": value["status"], "checked_at": timestamp}
    if "reason" in value:
        result["reason"] = code(value["reason"])
    source = value.get("source")
    require(isinstance(source, dict))
    if "error" in source:
        result["source"] = {"error": code(source["error"])}
    else:
        digest = source.get("sha256")
        require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest))
        result["source"] = {"sha256": digest, "xml_rows": integer(source.get("xml_rows"), 5000),
                            "laguna_candidates": sequence(source.get("laguna_candidates"), feed_item, 25)}
    result["connections"] = sequence(value.get("connections"), connection, 2)
    result["full_account_listing_scan_verified"] = False
    result["account_feed_url_verified"] = False
    require(len(json.dumps(result, ensure_ascii=False).encode()) <= MAX_BYTES)
    return result

def main():
    try:
        with open(sys.argv[1], "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        require(len(raw) <= MAX_BYTES)
        lines = raw.decode("utf-8").splitlines()
        frames = [line[len(FRAME):] for line in lines if line.startswith(FRAME)]
        require(len(frames) == 1)
        result = normalize(json.loads(frames[0]))
    except (OSError, ValueError, TypeError, KeyError, IndexError, UnicodeError, InvalidOperation):
        print("AVITO_READONLY_AUDIT_UNAVAILABLE receipt_invalid")
        return 0
    print("AVITO_READONLY_AUDIT_RESULT " + json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
