"""Schema projection for private, bounded Avito audit receipts; no Django."""
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

try:
    from scripts.avito_message_diagnostics import project_diagnostics
except ModuleNotFoundError as exc:
    if exc.name not in {"scripts", "scripts.avito_message_diagnostics"}:
        raise
    # The deployed standalone CLI starts with scripts/ on sys.path.
    from avito_message_diagnostics import project_diagnostics


FRAME = "AVITO_READONLY_AUDIT_RECEIPT "
MAX_BYTES = 262144
STATES = {"active", "removed", "old", "blocked", "rejected"}
CODE_PATTERN = re.compile(r"(?:monitor_(?:scan_limit|pagination_invalid|status_invalid)|(?:provider|source)_http_[1-5][0-9]{2}|audit_[a-z_]{1,60}|autoload_report_[a-z_]{1,60}|source_[a-z_]{1,60}|provider_[a-z_]{1,60}|advance_[a-z_]{1,60})\Z")

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
    require(value.get("link_state") in {"exact_xml_match", "xml_id_ambiguous", "not_in_current_xml", "no_autoload_id", "xml_unavailable",
        "services_feed_match", "services_feed_identity_only", "services_feed_id_mismatch"})
    if value["link_state"].startswith("services_"):
        require(value.get("xml_matches") == [])
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
    positions = [row["repeated_row"] for row in samples]
    require(positions == sorted(set(positions)))
    require(not {row["first_row"] for row in samples}.intersection(positions))
    sampled_pages, same_page, cross_page = {}, 0, 0
    for row in samples:
        seen_pages = sampled_pages.setdefault(row["first_row"], {row["first_page"]})
        same_page += row["repeated_page"] in seen_pages
        cross_page += row["repeated_page"] not in seen_pages
        seen_pages.add(row["repeated_page"])
    require(same_page <= result["same_page_duplicate_rows"])
    require(cross_page <= result["cross_page_duplicate_rows"])
    require(sum(row["identical_payload"] for row in samples) <= result["identical_duplicate_rows"])
    require(sum(not row["identical_payload"] for row in samples) <= result["different_duplicate_rows"])
    result["samples"] = samples
    return result


def input_copy_proof(value):
    require(isinstance(value, dict) and value.get("source") == "provider_report_input_copy")
    require(value.get("state") in {"verified_xml", "unavailable", "unsupported"})
    require(value.get("content_kind") in {"xml", "xlsx", "other", "unknown"})
    suffixes = {"xml_verified", "scope_unavailable", "report_ineligible", "url_count", "url_invalid",
        "host_not_allowed", "address_unavailable", "budget_exhausted", "redirect_denied",
        "encoding_unsupported", "size_invalid", "too_large", "incomplete", "transport_failed",
        "xml_forbidden", "xml_invalid", "xml_structure", "xml_limit", "not_xml",
        "identity_mismatch", "source_changed", "report_changed", "report_recheck_failed", "check_failed"}
    fixed = value.get("code")
    require(isinstance(fixed, str) and (fixed in {"input_copy_" + suffix for suffix in suffixes}
            or re.fullmatch(r"input_copy_http_[1-5][0-9]{2}", fixed)))
    count, size = integer(value.get("identity_count"), 200), integer(value.get("byte_count"), 4194304)
    if value["state"] == "verified_xml":
        require(fixed == "input_copy_xml_verified" and value["content_kind"] == "xml" and count > 0 and size > 0)
    else:
        require(fixed != "input_copy_xml_verified")
    # SHA, account/org bindings, downloaded URLs and headers remain server-private.
    return {"source": "provider_report_input_copy", "state": value["state"], "code": fixed,
            "content_kind": value["content_kind"], "identity_count": count, "byte_count": size}


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
    strategy = value.get("read_strategy", "provider_pages")
    require(strategy in ("provider_pages", "shop_xml_id_queries"))
    result["read_strategy"] = strategy
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
    if "input_copy_proof" in value:
        result["input_copy_proof"] = input_copy_proof(value["input_copy_proof"])
    if "message_groups" in value:
        allowed_explanations = {
            "existing_listing_link": "Сообщение о привязке существующего объявления; само по себе исправления не требует.",
            "information": "Информационное сообщение Авито.",
            "action_required": "Авито сообщает о проблеме, требующей проверки и исправления.",
            "review_required": "Предупреждение требует просмотра; по одному коду или количеству причина не определяется.",
        }
        allowed_fields = {"Id", "AvitoId", "Title", "Description", "Category", "ServiceType", "ServiceSubtype",
                          "Address", "ContactPhone", "Images", "ImageUrls", "Price", "PriceList", "DateEnd", "TNVED"}
        def message_group(row):
            require(isinstance(row, dict) and row.get("category") in allowed_explanations)
            require(row.get("type") in {"error", "warning", "alarm", "info"})
            require(row.get("explanation") == allowed_explanations[row["category"]])
            reasons = row.get("reason_keywords")
            require(isinstance(reasons, list) and len(reasons) <= 9 and all(reason in {
                "номер телефона", "ТН ВЭД", "фотографии", "срок размещения", "не поддерживается",
                "категория", "обязательное поле", "контакты в описании", "существующее объявление"} for reason in reasons))
            require(type(row.get("binding_evidence")) is bool and type(row.get("action_evidence")) is bool)
            fields = row.get("fields")
            require(isinstance(fields, list) and len(fields) <= len(allowed_fields)
                    and all(field in allowed_fields for field in fields))
            result = {"type": row["type"], "code": integer(row.get("code"), 10**12),
                "rows": integer(row.get("rows"), 5000), "category": row["category"],
                "explanation": row["explanation"], "fields": fields,
                "reason_keywords": reasons, "binding_evidence": row["binding_evidence"], "action_evidence": row["action_evidence"]}
            if "diagnostics" in row:
                require(type(row.get("diagnostics_consistent")) is bool)
                result["diagnostics"] = project_diagnostics(row["diagnostics"])
                result["diagnostics_consistent"] = row["diagnostics_consistent"]
            return result
        result["message_groups"] = sequence(value["message_groups"], message_group, 200)
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
    if value.get("scope") == "services":
        require(value.get("state") == "not_applicable" and value.get("reason") == "services_have_no_physical_stock")
        return {"scope": "services", "state": "not_applicable", "reason": "services_have_no_physical_stock"}
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

def advance_access(value):
    require(isinstance(value, dict))
    if "error" in value:
        return {"error": code(value["error"])}
    require(value.get("available") is True and value.get("source") == "cpa_v3"
            and value.get("amount_logged") is False)
    return {"available": True, "source": "cpa_v3", "amount_logged": False}

def monitoring(value):
    require(isinstance(value, dict))
    if "error" in value:
        return {"error": code(value["error"])}
    keys = ("advance_configured", "advance_enabled", "advance_scope_valid",
            "tariff_configured", "tariff_enabled", "tariff_scope_valid")
    nullable = ("advance_in_app_enabled", "advance_notification_exists", "tariff_in_app_enabled")
    require(value.get("amount_logged") is False and value.get("recipient_logged") is False)
    require(all(type(value.get(key)) is bool for key in keys))
    require(all(value.get(key) is None or type(value.get(key)) is bool for key in nullable))
    return {**{key: value[key] for key in keys},
            **{key: value.get(key) for key in nullable},
            "amount_logged": False, "recipient_logged": False}

def cached_configuration(value):
    """Explicit cached evidence; no callback URL, token, dates or feed contents."""
    require(isinstance(value, dict))
    if "error" in value:
        return {"error": code(value["error"])}
    require(value.get("source") == "service2_cached_configuration")
    require(value.get("live_provider_verified") is False)
    require(value.get("webhook_scope_known") is False)
    require(value.get("webhook_cache_status") in {"connected", "not_connected", "needs_check", "error", "unknown"})
    keys = ("webhook_cache_present", "webhook_configured_cached", "webhook_verified_cached",
            "webhook_received_cached", "autoload_auto_configured", "autoload_auto_enabled",
            "autoload_auto_scope_valid", "services_feed_configured", "services_feed_account_bound",
            "services_feed_enabled", "services_feed_revision_current", "services_feed_legacy_photo_limit_exceeded")
    require(all(value.get(key) is None or type(value.get(key)) is bool for key in keys))
    require(all(value.get(key) is None for key in (
        "webhook_configured_cached", "webhook_verified_cached", "webhook_received_cached")))
    return {"source": "service2_cached_configuration", "live_provider_verified": False,
            "webhook_scope_known": False, "webhook_cache_status": value["webhook_cache_status"],
            **{key: value.get(key) for key in keys}}


def runtime_checks(value):
    require(isinstance(value, list) and len(value) <= 6)
    names = {"statuses", "advance", "autoload_report", "tariff", "publication_correlation", "messages_digest"}
    outcomes = {"baseline", "success", "failed", "skipped", "not_run"}
    allowed_codes = {
    "", "not_configured", "disabled", "not_checked", "check_due", "schedule_invalid",
    "binding_changed", "check_error_unclassified", "worker_check_exception",
    "budget_or_batch_deferred", "workspace_busy", "worker_result_unknown", "quiet_hours", "not_due",
    "advance_binding_changed", "advance_data_invalid", "advance_provider_invalid", "advance_read_failed",
    "autoload_auto_binding_changed", "autoload_auto_error", "autoload_report_invalid",
    "autoload_report_changed", "autoload_report_limit", "autoload_report_duplicate_rows",
    "autoload_report_pagination", "provider_cooldown", "provider_unavailable",
    "provider_credentials_missing", "provider_credentials_invalid", "provider_token_missing",
    "provider_account_mismatch", "provider_account_id_invalid", "provider_account_id_missing",
    "provider_invalid_json", "provider_invalid_response", "provider_response_too_large",
    "monitor_scan_limit", "monitor_pagination_invalid", "monitor_status_invalid",
    "monitor_owner_unavailable", "monitor_connection_changed", "monitor_interrupted", "monitor_error",
    "tariff_reminder_failed", "tariff_notification_preferences_disabled", "notification_preferences_disabled",
    "publication_correlation_failed", "dependency_failed", "deferred_queue_invalid", "conversation_access_denied",
}
    result = []
    def timestamp(raw):
        if raw is None:
            return None
        require(isinstance(raw, str) and len(raw) <= 50 and datetime.fromisoformat(raw).tzinfo is not None)
        return raw
    for row in value:
        require(isinstance(row, dict) and row.get("check") in names)
        require(row.get("state") in {"disabled", "blocked", "failed", "pending", "due", "ok"})
        require(type(row.get("configured")) is bool and type(row.get("enabled")) is bool)
        require(row.get("scope_valid") is None or type(row.get("scope_valid")) is bool)
        require(type(row.get("last_worker_verified")) is bool)
        require(row.get("last_worker_outcome") is None or row["last_worker_outcome"] in outcomes)
        def fixed_code(raw):
            require(raw is None or (isinstance(raw, str) and (raw in allowed_codes or re.fullmatch(r"provider_http_[1-5][0-9]{2}", raw))))
            return raw
        result.append({"check": row["check"], "state": row["state"],
            "configured": row["configured"], "enabled": row["enabled"], "scope_valid": row["scope_valid"],
            "code": fixed_code(row.get("code")), "checked_at": timestamp(row.get("checked_at")),
            "success_at": timestamp(row.get("success_at")), "next_due_at": timestamp(row.get("next_due_at")),
            "last_worker_verified": row["last_worker_verified"],
            "last_worker_outcome": row.get("last_worker_outcome"),
            "last_worker_code": fixed_code(row.get("last_worker_code")),
            "last_worker_checked_at": timestamp(row.get("last_worker_checked_at"))})
    require(len({row["check"] for row in result}) == len(result))
    return result


def webhook_evidence(value):
    require(isinstance(value, dict))
    if "error" in value:
        return {"error": code(value["error"])}
    names = ("binding_valid", "registration_confirmed", "delivery_observed", "live_provider_checked")
    require(all(type(value.get(key)) is bool for key in names))
    require(value.get("live_registered") is None or type(value.get("live_registered")) is bool)
    require(not value["registration_confirmed"] or value["binding_valid"])
    require(not value["delivery_observed"] or (value["binding_valid"] and value["registration_confirmed"]))
    require(not value["live_provider_checked"] or (value["binding_valid"] and type(value["live_registered"]) is bool))
    require(value["live_provider_checked"] or value.get("live_registered") is None)
    return {**{key: value[key] for key in names}, "live_registered": value.get("live_registered")}


def storage_probe(value):
    require(isinstance(value, dict) and value.get("source") == "passenger_http")
    require(type(value.get("checked")) is bool and type(value.get("cached")) is bool)
    result = {"source": "passenger_http", "checked": value["checked"], "cached": value["cached"]}
    if value["checked"]:
        flags = ("outside_checkout", "file_private", "directory_private", "readable", "synthetic_only")
        require(all(value.get(flag) is True for flag in flags))
        result.update({flag: True for flag in flags})
    else:
        error = value.get("error")
        require(error in {"storage_probe_binding_changed", "storage_probe_not_run", "storage_probe_failed", "storage_probe_http_failed"} or
                isinstance(error, str) and re.fullmatch(r"storage_probe_http_(?:301|302|303|307|308|401|403|404|405|429|500|502|503|504)", error))
        result["error"] = error
    return result


def services_coverage(value):
    require(isinstance(value, dict) and value.get("source") == "service2_services_feed")
    require(type(value.get("complete")) is bool)
    result = {"source": "service2_services_feed", "complete": value["complete"]}
    local = ("draft_count", "published_count", "requested_count", "checked_count", "skipped_out_of_scope")
    provider = ("active_count", "active_covered", "active_missing", "published_inactive")
    for key in local:
        result[key] = integer(value.get(key), 5000)
    for key in provider:
        item = value.get(key)
        require(value["complete"] or item is None or type(item) is int)
        result[key] = integer(item, 5000) if item is not None else None
        require(not value["complete"] or result[key] is not None)
    if value["complete"]:
        require(result["active_covered"] + result["active_missing"] == result["active_count"])
    require(result["checked_count"] <= result["requested_count"])
    require(result["skipped_out_of_scope"] <= result["requested_count"])
    return result


def connection(value):
    require(isinstance(value, dict))
    result = {"connection_id": integer(value.get("connection_id"))}
    if "error" in value:
        return result | {"error": code(value["error"])}
    result.update(account_id=identifier(value.get("account_id")),
                  items=sequence(value.get("items"), item, 10), autoload_report=report(value.get("autoload_report")),
                  listing_scan=listing_scan(value.get("listing_scan", {"error": "audit_listing_scan_not_requested"})),
                  active_source_audit=inventory(value.get("active_source_audit", {"error": "audit_inventory_not_requested"})),
                  configured_feed_source=configured_feed_source(value.get("configured_feed_source", {"error": "audit_profile_source_not_requested"})),
                  advance_access=advance_access(value.get("advance_access", {"error": "audit_advance_not_requested"})),
                  monitoring=monitoring(value.get("monitoring", {"error": "audit_monitoring_not_requested"})),
                  cached_configuration=cached_configuration(value.get("cached_configuration", {"error": "audit_cached_configuration_not_requested"})))
    if "services_coverage" in value:
        result["services_coverage"] = services_coverage(value["services_coverage"])
    if "runtime_checks" in value:
        result["runtime_checks"] = runtime_checks(value["runtime_checks"])
    if "webhook_evidence" in value:
        result["webhook_evidence"] = webhook_evidence(value["webhook_evidence"])
    if "storage_probe" in value:
        result["storage_probe"] = storage_probe(value["storage_probe"])
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
