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
CODE_PATTERN = re.compile(r"(?:monitor_(?:scan_limit|pagination_invalid|status_invalid)|(?:provider|source)_http_[1-5][0-9]{2}|audit_[a-z_]{1,60}|autoload_report_[a-z_]{1,60}|source_[a-z_]{1,60}|provider_[a-z_]{1,60})\Z")

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
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,13}(?:\.[0-9]{1,6})?", value))
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

def feed_source_diagnostic(row):
    require(isinstance(row, dict))
    options = {"shape": {"object_url", "string", "unsupported"},
               "scheme": {"http", "https", "unknown"},
               "host_kind": {"shop", "shop_www", "shop_legacy", "service2", "info_site", "avito", "other", "unknown"},
               "path_kind": {"aggregate_file", "profile_43_file", "index_route", "root", "other", "unknown"}}
    projected = {}
    for key, allowed in options.items():
        require(row.get(key) in allowed)
        projected[key] = row[key]
    for key in ("query_present", "fragment_present", "credentials_present"):
        require(type(row.get(key)) is bool)
        projected[key] = row[key]
    return projected


def duplicate_report_diagnostics(value):
    require(isinstance(value, dict) and value.get("complete") is False)
    require(value.get("upload_status") in ("processing", "success", "success_warning", "error", ""))
    result = {"complete": False, "upload_id": identifier(value.get("upload_id")),
              "upload_status": value["upload_status"]}
    started_at = value.get("started_at")
    if started_at is not None:
        require(isinstance(started_at, str) and len(started_at) <= 40)
        require(datetime.fromisoformat(started_at).utcoffset() == timedelta(hours=7))
    result["started_at"] = started_at
    for key in ("read_rows", "unique_identity_pairs", "duplicate_extra_rows",
                "same_page_duplicate_rows", "cross_page_duplicate_rows",
                "identical_duplicate_rows", "different_duplicate_rows"):
        result[key] = integer(value.get(key), 5000)
    rows, extra = result["read_rows"], result["duplicate_extra_rows"]
    require(0 < extra < rows and result["unique_identity_pairs"] + extra == rows)
    require(result["same_page_duplicate_rows"] + result["cross_page_duplicate_rows"] == extra)
    require(result["identical_duplicate_rows"] + result["different_duplicate_rows"] == extra)
    pages = result["pages"] = integer(value.get("pages"), 100)
    require(pages == max(1, (rows + 49) // 50))
    def sample(row):
        require(isinstance(row, dict) and type(row.get("identical_payload")) is bool)
        first = integer(row.get("first_row"), rows)
        repeated = integer(row.get("repeated_row"), rows)
        require(1 <= first < repeated <= rows)
        first_page = integer(row.get("first_page"), pages)
        repeated_page = integer(row.get("repeated_page"), pages)
        require(first_page == (first - 1) // 50 + 1 and repeated_page == (repeated - 1) // 50 + 1)
        return {"first_row": first, "repeated_row": repeated, "first_page": first_page,
                "repeated_page": repeated_page, "identical_payload": row["identical_payload"]}
    samples = sequence(value.get("samples"), sample, 5)
    require(len(samples) == min(extra, 5))
    require(len({row["repeated_row"] for row in samples}) == len(samples))
    require(sum(row["identical_payload"] for row in samples) <= result["identical_duplicate_rows"])
    require(sum(not row["identical_payload"] for row in samples) <= result["different_duplicate_rows"])
    result["samples"] = samples
    return result


def report(value):
    require(isinstance(value, dict))
    if "error" in value:
        result = {"error": code(value["error"])}
        if result["error"] == "autoload_report_duplicate_rows" and "duplicate_diagnostics" in value:
            result["duplicate_diagnostics"] = duplicate_report_diagnostics(value["duplicate_diagnostics"])
        return result
    require(value.get("complete") is True and type(value.get("finished")) is bool)
    require(value.get("upload_status") in ("processing", "success", "success_warning", "error", ""))
    result = {"upload_id": identifier(value.get("upload_id")), "upload_status": value["upload_status"],
              "complete": True, "finished": value["finished"], "total": integer(value.get("total"), 5000)}
    result["counts"] = {key: integer(value.get("counts", {}).get(key, 0), 5000) for key in ("error", "warning", "alarm", "info")}
    require(all(count <= result["total"] for count in result["counts"].values()))
    result["critical_samples"] = sequence(value.get("critical_samples"), lambda row: {
        "xml_id": xml_id(row.get("xml_id")), "avito_id": identifier(row["avito_id"]) if row.get("avito_id") is not None else None,
        "messages": sequence(row.get("messages"), issue, 3)}, 25)
    started_at = value.get("started_at")
    if started_at is not None:
        require(isinstance(started_at, str) and len(started_at) <= 40)
        require(datetime.fromisoformat(started_at).utcoffset() == timedelta(hours=7))
    result["started_at"] = started_at
    source_kind = value.get("shop_feed_source", "other_or_unknown")
    require(source_kind in ("shop_aggregate_file", "shop_profile_43_file", "other_or_unknown"))
    result["shop_feed_source"] = source_kind
    result["shop_feed_diagnostics"] = sequence(value.get("shop_feed_diagnostics", []), feed_source_diagnostic, 50)
    if "message_code_counts" in value:
        def message_count(row):
            require(isinstance(row, dict) and row.get("type") in ("error", "warning", "alarm", "info"))
            count = integer(row.get("rows"), 5000)
            require(0 < count <= result["counts"][row["type"]])
            return {"type": row["type"], "code": integer(row.get("code"), 10**12), "rows": count}
        groups = sequence(value["message_code_counts"], message_count, 200)
        require(len({(row["type"], row["code"]) for row in groups}) == len(groups))
        result["message_code_counts"] = groups
    return result

def listing_scan(value):
    require(isinstance(value, dict))
    if "error" in value:
        return {"error": code(value["error"])}
    require(value.get("complete") is True)
    total = integer(value.get("total"), 5000)
    pages = integer(value.get("pages"), 100)
    require(pages > 0)
    counts = {status: integer(value.get("counts", {}).get(status), 5000) for status in sorted(STATES)}
    require(sum(counts.values()) == total)
    def duplicate(row):
        require(isinstance(row, dict))
        digest = row.get("title_sha256")
        require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest))
        active_count = integer(row.get("active_count"), 5000)
        ids = sequence(row.get("item_ids"), identifier, 10)
        require(2 <= active_count <= counts["active"] and len(ids) == min(active_count, 10) and len(set(ids)) == len(ids))
        return {"title_sha256": digest, "active_count": active_count, "item_ids": ids}
    candidates = sequence(value.get("duplicate_candidates"), duplicate, 25)
    group_count = integer(value.get("duplicate_title_group_count"), 2500)
    require(len(candidates) == min(group_count, 25) and group_count * 2 <= counts["active"])
    require(len({row["title_sha256"] for row in candidates}) == len(candidates))
    return {"complete": True, "total": total, "pages": pages, "counts": counts,
            "duplicate_title_group_count": group_count, "duplicate_candidates": candidates}

INVENTORY_LABELS = {
    "report_xml_match": "Сопоставлено с XML",
    "not_in_last_report": "Нет в последнем отчёте",
    "not_in_xml": "Нет в текущем XML",
    "ambiguous": "Неоднозначное сопоставление",
    "invalid_xml_id": "Некорректный XML ID",
}

def inventory(value):
    require(isinstance(value, dict))
    if "error" in value:
        return {"error": code(value["error"])}
    require(value.get("complete") is True)
    total = integer(value.get("total"), 500)
    def row(raw):
        require(isinstance(raw, dict) and raw.get("link_state") in INVENTORY_LABELS)
        amount = raw.get("shop_quantity")
        require(amount is None or (type(amount) is int and -10**9 <= amount <= 10**9))
        availability = raw.get("availability")
        require(availability in (None, "В наличии", "Под заказ"))
        state = raw["link_state"]
        linked = xml_id(raw.get("xml_id"))
        require(state != "report_xml_match" or linked is not None)
        require(state == "report_xml_match" or (amount is None and availability is None))
        return {"item_id": identifier(raw.get("item_id")), "link_state": state,
                "link_label": INVENTORY_LABELS[state], "xml_id": linked,
                "availability": availability, "shop_quantity": amount}
    rows = sequence(value.get("rows"), row, 500)
    require(len(rows) == total and len({row["item_id"] for row in rows}) == total)
    counts = {key: sum(row["link_state"] == key for row in rows) for key in INVENTORY_LABELS}
    require(value.get("counts") == counts)
    known = sum(row["shop_quantity"] is not None for row in rows)
    nonpositive = sum(row["shop_quantity"] is not None and row["shop_quantity"] <= 0 for row in rows)
    contradiction = sum(row["shop_quantity"] is not None and row["shop_quantity"] <= 0
                        and row["availability"] == "В наличии" for row in rows)
    preorder = sum(row["availability"] == "Под заказ" for row in rows)
    for key, expected in (("quantity_verified_rows", known), ("nonpositive_shop_quantity_rows", nonpositive),
                          ("in_stock_nonpositive_rows", contradiction), ("preorder_rows", preorder)):
        require(type(value.get(key)) is int and value[key] == expected)
    result = {"complete": True, "total": total, "rows": rows, "counts": counts,
              "quantity_verified_rows": known, "nonpositive_shop_quantity_rows": nonpositive,
              "in_stock_nonpositive_rows": contradiction, "preorder_rows": preorder,
              "saved_shop_stock_comparison_complete": known == total}
    if "stock_error" in value:
        result["stock_error"] = code(value["stock_error"])
    result["physical_stock_verified"] = False
    result["account_feed_url_verified"] = False
    return result

def configured_feed_source(value):
    require(isinstance(value, dict))
    if "error" in value:
        return {"error": code(value["error"])}
    require(value.get("complete") is True and type(value.get("autoload_enabled")) is bool)
    require(value.get("shop_feed_source") in ("shop_aggregate_file", "shop_profile_43_file", "other_or_unknown"))
    return {"complete": True, "autoload_enabled": value["autoload_enabled"],
            "shop_feed_source": value["shop_feed_source"],
            "shop_feed_diagnostics": sequence(value.get("shop_feed_diagnostics"), feed_source_diagnostic, 50)}

def connection(value):
    require(isinstance(value, dict))
    result = {"connection_id": integer(value.get("connection_id"))}
    if "error" in value:
        return result | {"error": code(value["error"])}
    result.update(account_id=identifier(value.get("account_id")),
                  items=sequence(value.get("items"), item, 10), autoload_report=report(value.get("autoload_report")),
                  listing_scan=listing_scan(value.get("listing_scan", {"error": "audit_listing_scan_not_requested"})),
                  active_source_audit=inventory(value.get("active_source_audit", {"error": "audit_inventory_not_requested"})),
                  configured_feed_source=configured_feed_source(value.get("configured_feed_source", {"error": "audit_profile_source_not_requested"})))
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
    result["full_account_listing_scan_verified"] = bool(result["connections"]) and all(
        "error" not in row and row["listing_scan"].get("complete") is True for row in result["connections"])
    result["listing_stock_reconciliation_verified"] = False
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
