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


def service_message_presentation(value, parent):
    require(isinstance(value, dict) and value.get("schema_version") == 1)
    require(type(value.get("schema_version")) is int and type(value.get("connection_id")) is int)
    require(value.get("connection_id") == 4 and value.get("account_id") == "446312402")
    require(value.get("upload_id") == parent["upload_id"])
    result = {"schema_version": 1, "connection_id": 4, "account_id": "446312402",
              "upload_id": identifier(value.get("upload_id"))}
    for name in ("row_count", "actionable_warning_rows", "error_rows", "alarm_rows",
                 "raw_warning_rows", "raw_info_rows"):
        result[name] = integer(value.get(name), 5000)
    require(result["row_count"] == parent["total"])
    require(result["actionable_warning_rows"] <= result["raw_warning_rows"])
    require(result["raw_warning_rows"] == parent["counts"]["warning"])
    require(result["raw_info_rows"] == parent["counts"]["info"])
    require(result["error_rows"] == parent["counts"]["error"])
    require(result["alarm_rows"] == parent["counts"]["alarm"])
    for name, rows in (("service_notice_messages", result["raw_warning_rows"]),
                       ("info_3002_messages", result["raw_info_rows"])):
        result[name] = integer(value.get(name), 250000)
        require(result[name] <= rows * 50)
    require(value.get("raw_snapshot_unchanged") is True)
    result["raw_snapshot_unchanged"] = True
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
    if "service_message_presentation" in value:
        result["service_message_presentation"] = service_message_presentation(value["service_message_presentation"], result)
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
    require(isinstance(value, list) and len(value) <= 7)
    names = {"statuses", "advance", "autoload_report", "tariff", "publication_correlation", "messages_digest", "statistics_daily"}
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
    "statistics_binding_changed", "statistics_data_invalid", "statistics_total_unavailable", "statistics_limit",
    "statistics_unavailable", "statistics_incomplete", "publication_correlation_failed", "dependency_failed", "deferred_queue_invalid", "conversation_access_denied",
}
    result = []
    def timestamp(raw):
        if raw is None:
            return None
        require(isinstance(raw, str) and len(raw) <= 50 and datetime.fromisoformat(raw).tzinfo is not None)
        return raw
    for row in value:
        require(isinstance(row, dict) and row.get("check") in names)
        require(row.get("state") in {"disabled", "blocked", "failed", "pending", "due", "ok"}
            or row.get("state") == "partial" and row["check"] == "statistics_daily"
                and row.get("code") in {"statistics_fields_unavailable", "statistics_total_unavailable"})
        require(type(row.get("configured")) is bool and type(row.get("enabled")) is bool)
        require(row.get("scope_valid") is None or type(row.get("scope_valid")) is bool)
        require(type(row.get("last_worker_verified")) is bool)
        require(row.get("last_worker_outcome") is None or row["last_worker_outcome"] in outcomes)
        def fixed_code(raw):
            require(raw is None or (isinstance(raw, str) and (raw in allowed_codes
                or row["check"] == "statistics_daily" and raw in {"provider_data_invalid", "statistics_fields_unavailable", "statistics_provider_error"}
                or re.fullmatch(r"provider_http_[1-5][0-9]{2}", raw))))
            return raw
        guard_names = {"queried_rows_changed", "shop_xml_snapshot_changed",
                       "profile_snapshot_changed", "summary_snapshot_changed"}
        def fixed_guard(raw):
            require(raw is None or isinstance(raw, str) and raw in guard_names)
            return raw
        saved_guard, worker_guard = fixed_guard(row.get("consistency_guard")), fixed_guard(row.get("last_worker_consistency_guard"))
        require(saved_guard is None or row["check"] == "autoload_report" and row.get("code") == "autoload_report_changed")
        require(worker_guard is None or row["check"] == "autoload_report" and row["last_worker_verified"]
                and row.get("last_worker_outcome") == "failed" and row.get("last_worker_code") == "autoload_report_changed")
        result.append({"check": row["check"], "state": row["state"],
            "configured": row["configured"], "enabled": row["enabled"], "scope_valid": row["scope_valid"],
            "code": fixed_code(row.get("code")), "checked_at": timestamp(row.get("checked_at")),
            "success_at": timestamp(row.get("success_at")), "next_due_at": timestamp(row.get("next_due_at")),
            "last_worker_verified": row["last_worker_verified"],
            "last_worker_outcome": row.get("last_worker_outcome"),
            "last_worker_code": fixed_code(row.get("last_worker_code")),
            "last_worker_checked_at": timestamp(row.get("last_worker_checked_at")),
            "consistency_guard": saved_guard, "last_worker_consistency_guard": worker_guard})
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



def release_timestamp(value):
    if value is None:
        return None
    require(isinstance(value, str) and len(value) <= 40)
    parsed = datetime.fromisoformat(value)
    require(parsed.utcoffset() is not None)
    return value



def raw_content_fields(value):
    require(isinstance(value, dict))
    if "error" in value:
        return {"error": code(value["error"])}
    require(value.get("source") == "avito_item_detail_raw_fields"
            and value.get("endpoint_kind") == "core_v1_account_item"
            and value.get("owned_status_verified") is True and value.get("sample_count") == 1
            and type(value.get("sample_count")) is int and value.get("live_content_verified") is False)
    fields = value.get("fields")
    require(isinstance(fields, dict))
    projected = {}
    for key in ("title", "description", "images", "photos"):
        raw = fields.get(key)
        require(isinstance(raw, dict))
        projected[key] = {}
        for spelling in ("lowercase", "capitalized"):
            field = raw.get(spelling)
            require(isinstance(field, dict) and type(field.get("present")) is bool)
            kind, count = field.get("type"), field.get("count")
            require(kind in {"missing", "null", "boolean", "string", "array", "object", "number"})
            require(field["present"] is (kind != "missing"))
            if kind in {"array", "object"}:
                count = integer(count, 1000)
            else:
                require(count is None)
            projected[key][spelling] = {"present": field["present"], "type": kind, "count": count}
    checked_at = release_timestamp(value.get("checked_at"))
    require(checked_at is not None)
    return {"source": value["source"], "endpoint_kind": value["endpoint_kind"],
        "owned_status_verified": True, "sample_count": 1, "live_content_verified": False,
        "checked_at": checked_at, "fields": projected}


def publication_release(value):
    require(isinstance(value, dict))
    if "error" in value:
        require(value["error"] == "audit_publication_release_unavailable")
        return {"error": value["error"]}
    flags = ("configured", "binding_present", "http_checked")
    result = {}
    for key in flags:
        require(type(value.get(key)) is bool)
        result[key] = value[key]
    for key in ("snapshot_valid", "current_publication", "draft_matches_published",
                "identity_sets_match", "photo_limit_ok"):
        item = value.get(key)
        require(item is None or type(item) is bool)
        result[key] = item
    require(value.get("snapshot_source") in {
        "unavailable", "current_published_without_history", "published_history", "legacy_history"})
    result["snapshot_source"] = value["snapshot_source"]
    result["revision"] = integer(value.get("revision"), 1000000000)
    for key in ("draft_revision", "published_revision"):
        revision = value.get(key)
        require(revision is None or type(revision) is int and 0 <= revision <= 1000000000)
        result[key] = revision
    history_id = value.get("history_version_id")
    require(history_id is None or type(history_id) is int and history_id > 0)
    result["history_version_id"] = history_id
    for key in ("created_at", "checked_at"):
        result[key] = release_timestamp(value.get(key))
    for key in ("draft_count", "published_count", "snapshot_count"):
        item = value.get(key)
        require(item is None or type(item) is int and 0 <= item <= 200)
        result[key] = item
    max_photos = value.get("max_photos")
    require(max_photos is None or type(max_photos) is int and 0 <= max_photos <= 30)
    result["max_photos"] = max_photos
    matched = value.get("http_matches")
    require(matched is None or type(matched) is bool)
    result["http_matches"] = matched
    require(value.get("http_code") in {
        "not_checked", "verified", "publication_missing", "scope_invalid",
        "snapshot_invalid", "http_unavailable", "http_mismatch", "scope_changed"})
    result["http_code"] = value["http_code"]
    facts = value.get("facts")
    require(isinstance(facts, dict))
    fact_names = ("file_published", "upload_found", "source_confirmed",
                  "rows_processed", "live_content_verified")
    require(all(type(facts.get(key)) is bool for key in fact_names))
    require(facts["live_content_verified"] is False)
    result["facts"] = {key: facts[key] for key in fact_names}
    return result


def webhook_chain(value):
    require(isinstance(value, dict))
    if "error" in value:
        require(value["error"] == "audit_webhook_chain_unavailable")
        return {"error": value["error"]}
    require(value.get("source") == "avito_webhook_chain_v1")
    require(value.get("state") in {
        "awaiting_first", "binding_unverified", "proof_invalid", "accepted_persisted", "persisted_row_missing"})
    result = {"source": value["source"], "state": value["state"]}
    for key in ("generation_bound", "authenticated_event", "conversation_bound",
                "message_persisted", "owner_notification_recorded"):
        require(type(value.get(key)) is bool)
        result[key] = value[key]
    require(value.get("push_delivery_confirmed") is False)
    result["push_delivery_confirmed"] = False
    require(value.get("event_result") in (None, "created", "duplicate"))
    result["event_result"] = value.get("event_result")
    require(value.get("owner_notification_state") in {
        "awaiting_first", "not_checked", "direct_recorded", "quiet_digest_recorded",
        "queued", "not_recorded", "not_eligible"})
    result["owner_notification_state"] = value["owner_notification_state"]
    require(result["owner_notification_recorded"] ==
            (result["owner_notification_state"] in {"direct_recorded", "quiet_digest_recorded"}))
    require(value.get("owner_notification_error") in {
        None, "notification_preferences_disabled", "conversation_access_denied",
        "binding_changed", "deferred_queue_invalid", "queue_error_unknown"})
    result["owner_notification_error"] = value.get("owner_notification_error")
    result["received_at"] = release_timestamp(value.get("received_at"))
    accepted = result["state"] == "accepted_persisted"
    require(result["conversation_bound"] is accepted and result["message_persisted"] is accepted)
    require(not result["authenticated_event"] or result["generation_bound"])
    require(not result["owner_notification_recorded"] or accepted)
    return result


def approved_photo(value):
    require(isinstance(value, dict))
    if "error" in value:
        require(value["error"] == "audit_approved_photo_unavailable")
        return {"error": value["error"]}
    require(value.get("source") == "existing_services_draft_image")
    require(value.get("scope_valid") is True and value.get("readable") is True
            and value.get("source_in_published") is True)
    require(value.get("format") in {"JPEG", "PNG", "WEBP"})
    result = {"source": value["source"], "scope_valid": True, "readable": True,
              "source_in_published": True, "format": value["format"]}
    result["photo_count"] = integer(value.get("photo_count"), 10)
    require(result["photo_count"] > 0)
    for key in ("width", "height"):
        result[key] = integer(value.get(key), 20000000)
        require(result[key] > 0)
    require(result["width"] * result["height"] <= 20000000)
    result["bytes"] = integer(value.get("bytes"), 8 * 1024 * 1024)
    require(result["bytes"] > 0)
    result["checked_at"] = release_timestamp(value.get("checked_at"))
    require(result["checked_at"] is not None)
    upgraded = value.get("https_upgrade_followed")
    require(upgraded is None or type(upgraded) is bool)
    result["https_upgrade_followed"] = upgraded
    return result



MEDIA_READINESS_CHECKS = (
    "connection_scope", "actor_active", "editor_organization", "manage_channels",
    "setup_binding", "feed_binding", "published_revision", "shared_identity", "shared_photo",
    "photo_scope", "form_valid", "title_roundtrip", "description_roundtrip",
    "image_urls_roundtrip", "source_read", "image_decode",
)
MEDIA_READINESS_REASONS = {
    "media_ready", "media_connection_invalid", "media_actor_invalid",
    "media_editor_organization_mismatch", "media_capability_denied", "media_setup_binding_invalid",
    "media_feed_invalid", "media_published_revision_mismatch", "media_shared_identity_missing",
    "media_shared_photo_missing", "media_photo_scope_invalid", "media_form_invalid",
    "media_title_roundtrip_changed", "media_description_roundtrip_changed",
    "media_image_urls_roundtrip_changed", "media_source_read_failed", "media_image_decode_failed",
}



MEDIA_TRANSPORT_FIELDS = (
    "reason", "phase", "http_status", "source_scheme", "path_ascii", "https_upgrade_followed", "host_relation", "dns_family",
    "dns_answer_count", "selected_ip_family", "response_format", "declared_bytes",
    "received_bytes", "io_errno",
)
MEDIA_TRANSPORT_REASONS = {
    "url_invalid", "url_policy", "private_address", "dns_busy", "dns_timeout", "dns_failed",
    "dns_private_address", "dns_unavailable", "deadline", "http_status", "content_type",
    "content_length_invalid", "content_length_limit", "body_limit", "incomplete_body",
    "tls_certificate", "tls_error", "io_timeout", "io_error", "http_protocol", "request_encoding",
    "upgrade_source_scheme", "upgrade_location_missing", "upgrade_location_multiple",
    "upgrade_location_invalid", "upgrade_location_controls", "upgrade_location_fragment",
    "upgrade_location_userinfo", "upgrade_target_scheme", "upgrade_target_port",
    "upgrade_target_host", "upgrade_target_path", "upgrade_target_query", "upgrade_second_hop",
}


def media_transport(value):
    require(isinstance(value, dict) and all(key in value for key in MEDIA_TRANSPORT_FIELDS))
    result = {}
    allowed = {
        "reason": MEDIA_TRANSPORT_REASONS,
        "phase": {"url", "dns", "connect", "request", "headers", "body"},
        "source_scheme": {"http", "https"},
        "host_relation": {"same", "www_alias", "other", "unknown"},
        "dns_family": {"ipv4", "ipv6", "mixed"},
        "selected_ip_family": {"ipv4", "ipv6"},
        "response_format": {"jpeg", "png", "webp", "avif", "html", "json", "octet_stream", "other"},
    }
    for key, options in allowed.items():
        item = value[key]
        require(item is None or isinstance(item, str) and item in options)
        result[key] = item
    for key in ("path_ascii", "https_upgrade_followed"):
        item = value[key]
        require(item is None or type(item) is bool)
        result[key] = item
    for key, minimum, maximum in (
        ("http_status", 100, 599), ("dns_answer_count", 1, 32),
        ("declared_bytes", 0, 8 * 1024 * 1024 + 1),
        ("received_bytes", 0, 8 * 1024 * 1024 + 1), ("io_errno", 0, 4095),
    ):
        item = value[key]
        require(item is None or type(item) is int and minimum <= item <= maximum)
        result[key] = item
    return result


def media_readiness(value):
    require(isinstance(value, dict))
    if "error" in value:
        require(value["error"] == "audit_media_readiness_unavailable")
        return {"error": value["error"]}
    require(value.get("state") in {"ready", "blocked"}
            and value.get("stage") in {*MEDIA_READINESS_CHECKS, "complete"}
            and value.get("reason") in MEDIA_READINESS_REASONS)
    require(value.get("exception_class") in {
        None, "PermissionDenied", "Http404", "LiveMediaCheckError", "AvitoError", "ValueError",
        "OSError", "DatabaseError", "TypeError", "KeyError", "AttributeError", "OtherError"})
    checks = value.get("checks")
    require(isinstance(checks, dict))
    result = {key: value[key] for key in ("state", "stage", "reason", "exception_class")}
    result["checks"] = {}
    result["transport"] = media_transport(value.get("transport"))
    for key in MEDIA_READINESS_CHECKS:
        item = checks.get(key)
        require(key in checks and (item is None or type(item) is bool))
        result["checks"][key] = item
    if result["state"] == "ready":
        require(result["stage"] == "complete" and result["reason"] == "media_ready"
                and result["exception_class"] is None and all(item is True for item in result["checks"].values()))
    else:
        require(result["stage"] != "complete" and result["reason"] != "media_ready")
    return result


def operation_ledger(value):
    require(isinstance(value, dict))
    if "error" in value:
        require(value["error"] == "audit_operation_ledger_unavailable")
        return {"error": value["error"]}
    require(value.get("state") in {
        "not_started", "scope_invalid", "pending", "completed", "unknown", "failed", "blocked", "busy"})
    require(value.get("phase") in {
        "reserve", "publication_preflight", "account_read", "photo_source", "binding_check",
        "token_prepare", "http_pending", "http_post", "receipt_read", "receipt_validate", "finalize", "unknown"})
    require(value.get("exception_class") in {
        None, "Blocked", "AvitoError", "LiveMediaCheckError", "ValueError", "PermissionDenied", "Http404",
        "OSError", "TimeoutError", "HTTPError", "URLError", "JSONDecodeError", "OperationalError",
        "IntegrityError", "FieldError", "Other"})
    result = {key: value[key] for key in ("state", "phase", "exception_class")}
    flags = ("has_operation_id", "pending_http", "has_exact_receipt")
    for key in flags:
        item = value.get(key)
        require(item is None or type(item) is bool)
        result[key] = item
    if any(result[key] is None for key in flags):
        require(all(result[key] is None for key in flags) and result["state"] != "completed")
    else:
        require(not result["has_exact_receipt"] or result["has_operation_id"])
        require(result["pending_http"] == (result["has_operation_id"] and not result["has_exact_receipt"]))
        require(result["state"] != "completed" or result["has_exact_receipt"])
    return result



# Public metadata only: no provider payloads, metric values or working customer IDs.
STAT_METRIC_CODES = ('impressions', 'views', 'contacts', 'favorites', 'contactsShowPhone',
    'contactsMessenger', 'contactsShowPhoneAndMessenger', 'allSpending', 'spending',
    'presenceSpending', 'promoSpending', 'restSpending', 'commission')
STAT_GAP_STATES = {'no_completed_request', 'not_in_known_manifest', 'provider_omitted_row',
    'local_fact_missing', 'local_fact_mismatch', 'request_provenance_inconsistent',
    'provider_metrics_missing', 'known'}
STAT_INVALID_REASONS = frozenset({
    "result_missing", "result_type", "grouping_missing_with_explicit_zero_total",
    "groupings_type", "groupings_limit", "totals_multiple_groups", "group_type",
    "metrics_type", "id_type", "id_duplicate", "metric_type", "metric_slug_type",
    "metric_duplicate", "total_type"})
STAT_DIAGNOSTIC_REASONS = {
    "provider_data_invalid": STAT_INVALID_REASONS,
    "statistics_fields_unavailable": frozenset({"groupings_absent", "groupings_and_total_absent"}),
    "statistics_provider_error": frozenset({"provider_error_reported"}),
}
STAT_SHAPE_GROUPINGS = frozenset({
    "missing", "null", "empty_object", "object", "empty_list", "list",
    "string", "integer", "number", "bool", "other"})
STAT_SHAPE_TOTALS = frozenset({
    "missing", "null", "zero_integer", "positive_integer", "negative_integer",
    "string", "bool", "number", "other"})
STAT_SHAPE_KEYS = frozenset({"groupings_kind", "data_total_count_kind", "provider_error_present"})
STAT_ERROR_CODES = {'', 'statistics_binding_changed', 'statistics_data_invalid',
    'statistics_total_unavailable', 'statistics_limit', 'statistics_unavailable',
    'statistics_incomplete', 'provider_cooldown', 'provider_unavailable',
    'provider_response_too_large', 'provider_invalid_json', 'provider_invalid_response',
    'provider_data_invalid', 'statistics_fields_unavailable', 'statistics_provider_error'}


def _statistics_date(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    require(parsed.isoformat() == value)
    return parsed


def _statistics_error(value):
    require(isinstance(value, str) and (value in STAT_ERROR_CODES
        or re.fullmatch(r"provider_http_[1-5][0-9]{2}", value)))
    return value


def _statistics_shape(value):
    require(type(value) is dict and set(value) == STAT_SHAPE_KEYS
        and type(value["groupings_kind"]) is str and value["groupings_kind"] in STAT_SHAPE_GROUPINGS
        and type(value["data_total_count_kind"]) is str and value["data_total_count_kind"] in STAT_SHAPE_TOTALS
        and type(value["provider_error_present"]) is bool)
    return {key: value[key] for key in ("groupings_kind", "data_total_count_kind", "provider_error_present")}


def _statistics_attempt(value, first, last):
    require(isinstance(value, dict) and value.get("state") in {
        "observed", "historical_unknown", "not_observed", "unconfirmed"})
    state = value["state"]
    if state != "observed":
        require(value.get("phase") == "unknown" and "statistics_reason" not in value and "statistics_shape" not in value)
        return {"state": state, "phase": "unknown",
            "error_code": _statistics_error(value.get("error_code")),
            "finished_at": release_timestamp(value.get("finished_at"))}
    require(value.get("phase") in {
        "authorization", "fetch_statistics", "verify_account", "validate_response", "pagination"})
    require(value.get("exception_class") in {"", "AvitoCooldownError", "AvitoRetryableError",
        "AvitoError", "ValueError", "TypeError", "KeyError", "RuntimeError",
        "InvalidOperation", "unexpected_exception"})
    require(value.get("outcome") in {"success", "failed", "skipped"})
    require(first <= _statistics_date(value.get("source_date")) <= last)
    started, finished = release_timestamp(value.get("started_at")), release_timestamp(value.get("finished_at"))
    require(started is not None and finished is not None
        and datetime.fromisoformat(started) <= datetime.fromisoformat(finished))
    run_id = integer(value.get("run_id"))
    require(run_id > 0)
    result = {"state": "observed", "run_id": run_id, "source_date": value["source_date"],
        "offset": integer(value.get("offset"), 200), "phase": value["phase"],
        "exception_class": value["exception_class"], "outcome": value["outcome"],
        "error_code": _statistics_error(value.get("error_code")),
        "started_at": started, "finished_at": finished}
    if "statistics_reason" in value:
        reason = value["statistics_reason"]
        require(isinstance(reason, str) and reason in STAT_DIAGNOSTIC_REASONS.get(result["error_code"], ()))
        result["statistics_reason"] = reason
    if "statistics_shape" in value:
        require("statistics_reason" in result)
        result["statistics_shape"] = _statistics_shape(value["statistics_shape"])
    return result


def _statistics_gaps(value, cutoff):
    require(isinstance(value, dict) and type(value.get("schema_version")) is int and value["schema_version"] == 1)
    if value.get("state") == "blocked":
        require(value.get("code") == "statistics_binding_changed")
        return {"schema_version": 1, "state": "blocked", "code": value["code"]}
    require(value.get("state") == "observed" and value.get("source") == "avito_stats_v2_items"
        and type(value.get("connection_id")) is int and value["connection_id"] == 4
        and value.get("account_id") == "446312402" and value.get("avito_id") == "8450664985"
        and value.get("provider_timezone") == "unknown"
        and value.get("request_scope") == "all_account_items_no_item_filter"
        and value.get("unit_contract") == "stats_v2_confirmed_metrics_v1"
        and value.get("manifest_is_request_filter") is False)
    first = cutoff-timedelta(days=29)
    require(_statistics_date(value.get("from")) == first and _statistics_date(value.get("to")) == cutoff)
    require(type(value.get("expected_dates")) is int and value["expected_dates"] == 30)
    completed = integer(value.get("completed_dates"), 30)
    rows = value.get("dates")
    require(isinstance(rows, list) and len(rows) == 30)
    output = []
    for index, row in enumerate(rows):
        require(isinstance(row, dict) and row.get("grouping") == "item")
        day = cutoff-timedelta(days=index)
        require(all(_statistics_date(row.get(key)) == day for key in ("date", "date_from", "date_to")))
        require(row.get("state") in STAT_GAP_STATES)
        flags = ("pages_complete", "request_provenance_consistent", "stored_row_present",
            "stored_row_matches_run", "stored_row_consistent")
        nullable = ("expected_manifest_member", "provider_row_present")
        require(all(type(row.get(key)) is bool for key in flags))
        require(all(row.get(key) is None or type(row.get(key)) is bool for key in nullable))
        codes = {}
        for key in ("missing_metric_codes", "zero_metric_codes"):
            raw = row.get(key)
            require(isinstance(raw, list) and len(raw) <= len(STAT_METRIC_CODES)
                and all(isinstance(code, str) and code in STAT_METRIC_CODES for code in raw)
                and len(set(raw)) == len(raw))
            codes[key] = list(raw)
        known = integer(row.get("known_metric_count"), len(STAT_METRIC_CODES))
        require(known + len(codes["missing_metric_codes"]) == len(STAT_METRIC_CODES)
            and not set(codes["missing_metric_codes"]) & set(codes["zero_metric_codes"])
            and (not known or row["stored_row_consistent"] and row["provider_row_present"] is True))
        counts = {key: integer(row[key], 200) if row.get(key) is not None else None
            for key in ("expected_total", "received_rows")}
        ids = {}
        for key in ("latest_complete_run_id", "latest_attempt_run_id"):
            raw = row.get(key)
            ids[key] = integer(raw) if raw is not None else None
            require(ids[key] is None or ids[key] > 0)
        require(row.get("latest_attempt_state") in {None, "collecting", "success", "partial", "failed"})
        if "pages" in row:
            pages = row["pages"]
            require(isinstance(pages, list) and len(pages) <= 200)
            require(all(isinstance(page, dict)
                and type(page.get("offset")) is int and 0 <= page["offset"] <= 200
                and type(page.get("rows")) is int and 0 <= page["rows"] <= 50 for page in pages))
            page_count = len(pages)
        else:
            # The host already projects before the runner validates the same receipt.
            page_count = integer(row.get("page_count"), 200)
        completed_at = release_timestamp(row.get("completed_at"))
        require(row["pages_complete"] == (ids["latest_complete_run_id"] is not None)
            and (not row["pages_complete"] or completed_at is not None)
            and (not row["request_provenance_consistent"] or row["pages_complete"])
            and (not row["stored_row_matches_run"] or row["stored_row_present"] and row["pages_complete"])
            and (not row["stored_row_consistent"] or row["stored_row_matches_run"]))
        if row["state"] == "no_completed_request":
            require(not row["pages_complete"] and row["expected_manifest_member"] is None
                and row["provider_row_present"] is None)
        else:
            require(row["pages_complete"] and type(row["expected_manifest_member"]) is bool
                and type(row["provider_row_present"]) is bool)
            if row["state"] == "request_provenance_inconsistent":
                require(not row["request_provenance_consistent"])
            else:
                require(row["request_provenance_consistent"])
                if row["state"] == "not_in_known_manifest":
                    require(row["expected_manifest_member"] is False)
                else:
                    require(row["expected_manifest_member"] is True)
                    if row["state"] == "local_fact_missing":
                        require(not row["stored_row_present"])
                    elif row["state"] == "local_fact_mismatch":
                        require(row["stored_row_present"] and not row["stored_row_consistent"])
                    else:
                        require(row["stored_row_consistent"])
                        require((row["state"] == "provider_omitted_row" and row["provider_row_present"] is False and known == 0)
                            or (row["state"] == "provider_metrics_missing" and row["provider_row_present"] is True and known < len(STAT_METRIC_CODES))
                            or (row["state"] == "known" and row["provider_row_present"] is True and known == len(STAT_METRIC_CODES)))
        output.append({"date": day.isoformat(), "date_from": day.isoformat(), "date_to": day.isoformat(),
            "grouping": "item", "state": row["state"],
            **{key: row[key] for key in flags+nullable}, **codes, **counts, **ids,
            "page_count": page_count, "known_metric_count": known, "completed_at": completed_at,
            "latest_attempt_state": row.get("latest_attempt_state"),
            "latest_attempt_error_code": _statistics_error(row.get("latest_attempt_error_code")),
            "latest_attempt_finished_at": release_timestamp(row.get("latest_attempt_finished_at"))})
    states = value.get("states")
    require(isinstance(states, dict) and set(states) == STAT_GAP_STATES)
    projected_states = {key: integer(states[key], 30) for key in sorted(STAT_GAP_STATES)}
    require(all(projected_states[key] == sum(row["state"] == key for row in output) for key in STAT_GAP_STATES)
        and completed == sum(row["pages_complete"] for row in output))
    return {"schema_version": 1, "state": "observed", "source": "avito_stats_v2_items",
        "connection_id": 4, "account_id": "446312402", "avito_id": "8450664985",
        "provider_timezone": "unknown", "from": first.isoformat(), "to": cutoff.isoformat(),
        "request_scope": "all_account_items_no_item_filter", "unit_contract": "stats_v2_confirmed_metrics_v1",
        "manifest_is_request_filter": False, "expected_dates": 30, "completed_dates": completed,
        "states": projected_states, "dates": output,
        "last_attempt": _statistics_attempt(value.get("last_attempt"), first, cutoff)}


def statistics_source_evidence(value):
    """Strict projection of the existing saved-statistics helper, not a collector."""
    require(isinstance(value, dict))
    if "error" in value:
        require(value["error"] == "audit_statistics_source_evidence_unavailable")
        return {"error": value["error"]}
    require(type(value.get("schema_version")) is int and value["schema_version"] == 1)
    if value.get("state") == "blocked":
        require(value.get("code") == "statistics_binding_changed")
        return {"schema_version": 1, "state": "blocked", "code": value["code"]}
    require(value.get("state") == "observed" and value.get("source") == "avito_stats_v2_items"
        and value.get("provider_timezone") == "unknown"
        and value.get("barnaul_calendar_confirmed") is False
        and value.get("comparison_rule") == "all_requested_metrics_known"
        and type(value.get("expected_dates")) is int and value["expected_dates"] == 30)
    cutoff = _statistics_date(value.get("cutoff"))
    completed = integer(value.get("completed_dates"), 30)
    periods = value.get("periods")
    windows = (("day", 0, 0), ("previous_day", 1, 1), ("seven_days", 6, 0),
        ("previous_seven_days", 13, 7), ("thirty_days", 29, 0))
    require(isinstance(periods, list) and len(periods) == len(windows))
    output = []
    for row, (key, start, end) in zip(periods, windows):
        require(isinstance(row, dict) and row.get("key") == key
            and _statistics_date(row.get("from")) == cutoff-timedelta(days=start)
            and _statistics_date(row.get("to")) == cutoff-timedelta(days=end))
        missing = integer(row.get("missing_dates"), start-end+1)
        expected, reported = integer(row.get("expected_ad_cells"), 12000), integer(row.get("reported_ad_cells"), 6000)
        flags = ("pages_complete", "ad_rows_complete", "comparison_applicable", "same_cohort", "comparison_available")
        require(all(type(row.get(flag)) is bool for flag in flags)
            and row["pages_complete"] == (missing == 0)
            and row["comparison_applicable"] == (key != "thirty_days")
            and (not row["ad_rows_complete"] or row["pages_complete"] and expected > 0 and expected == reported)
            and (not row["comparison_available"] or row["comparison_applicable"] and row["same_cohort"] and row["pages_complete"]))
        output.append({"key": key, "from": row["from"], "to": row["to"], "missing_dates": missing,
            "expected_ad_cells": expected, "reported_ad_cells": reported,
            **{flag: row[flag] for flag in flags}})
    require(completed == 30-output[-1]["missing_dates"])
    return {"schema_version": 1, "state": "observed", "source": "avito_stats_v2_items",
        "provider_timezone": "unknown", "barnaul_calendar_confirmed": False,
        "comparison_rule": "all_requested_metrics_known", "cutoff": cutoff.isoformat(),
        "completed_dates": completed, "expected_dates": 30, "periods": output,
        "gap_diagnostics": _statistics_gaps(value.get("gap_diagnostics"), cutoff)}



def placement_finish_metadata(row):
    """Only closed shape/date evidence; historical rows without shape remain historical."""
    raw = row.get("finish_shape")
    if raw is None:
        require(row.get("finish_state") in {"known", "not_returned", "invalid"})
        return {}
    require(isinstance(raw, dict) and raw.get("stage") == "reader_normalized")
    kind, length, fmt = raw.get("value_type"), raw.get("length"), raw.get("format")
    require(kind in {"null", "boolean", "string", "integer", "number", "array", "object", "other"})
    require((type(length) is int and 0 <= length <= 51) if kind == "string" else length is None)
    require(fmt in {"missing", "empty", "not_text", "invalid", "over_limit",
                    "date_only", "naive_datetime", "unknown_offset_datetime", "aware_datetime"})
    state, calendar = row.get("finish_state"), row.get("source_finish_date")
    if calendar is not None:
        require(isinstance(calendar, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", calendar))
        require(datetime.fromisoformat(calendar).date().isoformat() == calendar)
    if state == "known":
        require(kind == "string" and 1 <= length <= 50 and fmt == "aware_datetime" and calendar is None
                and not re.search(r"-00(?::?00)?$", row.get("finish_time", "")))
    elif state == "timezone_missing":
        require(kind == "string" and 1 <= length <= 50 and fmt in {"naive_datetime", "unknown_offset_datetime"} and calendar is not None)
    elif state == "calendar_date_only":
        require(kind == "string" and length == 10 and fmt == "date_only" and calendar is not None)
    elif state == "not_returned":
        require((kind == "null" and fmt == "missing") or (kind == "string" and length == 0 and fmt == "empty"))
        require(calendar is None)
    elif state == "invalid_type":
        require(kind not in {"null", "string"} and fmt == "not_text" and calendar is None)
    elif state == "invalid_format":
        require(kind == "string" and ((fmt == "invalid" and 1 <= length <= 50)
                or (fmt == "over_limit" and length == 51)) and calendar is None)
    else:
        raise ValueError("invalid receipt")
    return {"source_finish_date": calendar,
        "finish_shape": {"stage": "reader_normalized", "value_type": kind, "length": length, "format": fmt}}


def placement_evidence(value):
    require(isinstance(value, dict))
    if "error" in value:
        return {"error": code(value["error"])}
    require(value.get("source") == "avito_core_v1_owned_item_detail"
            and type(value.get("connection_id")) is int and value["connection_id"] == 4
            and value.get("account_id") == "446312402")
    requested = integer(value.get("requested"), 20)
    checked = integer(value.get("checked"), 20)
    known = integer(value.get("terms_known"), 20)
    require(1 <= requested and known <= checked <= requested)
    require(type(value.get("complete")) is bool
            and value["complete"] == (checked == requested)
            and type(value.get("status_scan_complete")) is bool
            and value.get("provider_write_performed") is False
            and value.get("local_snapshot_changed") is False)
    raw_rows = value.get("rows")
    require(isinstance(raw_rows, list) and len(raw_rows) == requested)
    rows, ids = [], set()
    for row in raw_rows:
        require(isinstance(row, dict))
        item_id = identifier(row.get("avito_id"))
        require(item_id not in ids)
        ids.add(item_id)
        if "error" in row:
            rows.append({"avito_id": item_id, "error": code(row["error"])})
            continue
        require(row.get("status") in STATES
                and row.get("finish_state") in {"known", "not_returned", "invalid", "timezone_missing",
                    "calendar_date_only", "invalid_type", "invalid_format"}
                and row.get("identity_state") in {"matched", "not_returned"})
        checked_at = release_timestamp(row.get("checked_at"))
        require(checked_at is not None)
        finish = release_timestamp(row.get("finish_time"))
        require((finish is not None) == (row["finish_state"] == "known"))
        rows.append({"avito_id": item_id, "status": row["status"], "checked_at": checked_at,
            "finish_time": finish, "finish_state": row["finish_state"],
            "identity_state": row["identity_state"]} | placement_finish_metadata(row))
    require(sum("error" not in row for row in rows) == checked
            and sum(row.get("finish_state") == "known" for row in rows) == known)
    return {"source": value["source"], "connection_id": 4, "account_id": "446312402",
        "requested": requested, "checked": checked, "complete": value["complete"],
        "terms_known": known, "status_scan_complete": value["status_scan_complete"], "rows": rows,
        "provider_write_performed": False, "local_snapshot_changed": False}


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
    presentation = result["autoload_report"].get("service_message_presentation")
    if presentation is not None:
        require(result["connection_id"] == presentation["connection_id"]
                and result["account_id"] == presentation["account_id"])
    for key, project in (("publication_release", publication_release),
                         ("webhook_chain", webhook_chain), ("approved_photo", approved_photo),
                         ("raw_content_fields", raw_content_fields),
                         ("media_readiness", media_readiness), ("operation_ledger", operation_ledger)):
        if key in value:
            result[key] = project(value[key])
    if "placement_evidence" in value:
        try:
            require(result["connection_id"] == 4 and result["account_id"] == "446312402")
            result["placement_evidence"] = placement_evidence(value["placement_evidence"])
        except (ValueError, TypeError, KeyError, OverflowError):
            result["placement_evidence"] = {"error": "audit_placement_evidence_unavailable"}
    if "statistics_source_evidence" in value:
        try:
            require(result["connection_id"] == 4 and result["account_id"] == "446312402")
            result["statistics_source_evidence"] = statistics_source_evidence(value["statistics_source_evidence"])
        except (ValueError, TypeError, KeyError, OverflowError):
            # The optional child is unavailable; independent account proofs survive.
            result["statistics_source_evidence"] = {"error": "audit_statistics_source_evidence_unavailable"}
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
    scoped = "scope" in value
    if scoped:
        scope = value["scope"]
        require(isinstance(scope, dict) and scope.get("kind") == "services_readonly"
                and type(scope.get("connection_id")) is int and scope["connection_id"] == 4
                and scope.get("account_id") == "446312402"
                and type(scope.get("revision")) is int
                and 0 < scope["revision"] <= 1000000000)
        selection = scope.get("revision_source")
        require(selection in (None, "current_published"))
        require(selection == "current_published" or scope["revision"] == 15)
        result["scope"] = {"kind": "services_readonly", "connection_id": 4,
                           "account_id": "446312402", "revision": scope["revision"]}
        if selection is not None:
            result["scope"]["revision_source"] = selection
        require(value.get("prepared_owner_page") == "/avito/4/services-feed/")
        result["prepared_owner_page"] = "/avito/4/services-feed/"
    if "reason" in value:
        result["reason"] = code(value["reason"])
    source = value.get("source")
    require(isinstance(source, dict))
    if scoped:
        require(source.get("scope") == "services" and source.get("state") == "not_applicable"
                and source.get("reason") == "services_have_no_physical_stock")
        result["source"] = {"scope": "services", "state": "not_applicable",
                            "reason": "services_have_no_physical_stock"}
    elif "error" in source:
        result["source"] = {"error": code(source["error"])}
    else:
        digest = source.get("sha256")
        require(isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest))
        result["source"] = {"sha256": digest, "xml_rows": integer(source.get("xml_rows"), 5000),
                            "laguna_candidates": sequence(source.get("laguna_candidates"), feed_item, 25)}
    result["connections"] = sequence(value.get("connections"), connection, 1 if scoped else 2)
    if scoped:
        require(result["status"] == "blocked" or len(result["connections"]) == 1)
        for row in result["connections"]:
            require(row["connection_id"] == 4)
            if "error" not in row:
                require(row["account_id"] == "446312402")
                require(all(key in row for key in
                            ("publication_release", "webhook_chain", "approved_photo", "raw_content_fields")) )
                if "error" not in row["publication_release"]:
                    require(row["publication_release"]["revision"] == result["scope"]["revision"])
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
