"""Explicit hosting-operator diagnostics: reads Avito, never changes listings."""
import hashlib
import re
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from pool_service import avito_autoload_report, avito_source_audit, avito_workspace, avito_status_monitor, avito_inventory
from pool_service.communication_avito import AvitoError, access_token, authorized_account_id, _provider_root
from pool_service.communication_models import ChannelConnection, CommunicationAccess
from pool_service.models import OrganizationAccess
from scripts.avito_readonly_receipt import normalize, safe_text, xml_id, price

MAX_CONNECTIONS = 2
SAFE_ERRORS = {
    "provider_credentials_missing", "provider_credentials_invalid", "provider_token_missing",
    "audit_provider_root_invalid", "audit_provider_cooldown", "audit_report_code_limit",
    "monitor_scan_limit", "monitor_pagination_invalid", "monitor_status_invalid",
    "provider_account_id_missing", "provider_account_id_invalid", "provider_item_unavailable",
    "provider_account_mismatch", "provider_data_invalid", "provider_unavailable",
    "provider_response_too_large", "provider_invalid_json", "provider_invalid_response",
    "autoload_report_invalid", "autoload_report_changed", "autoload_report_limit", "autoload_report_pagination",
    *avito_source_audit.ERRORS,
}

def safe_error(error):
    value = str(error) if isinstance(error, AvitoError) else ""
    return value if value in SAFE_ERRORS or re.fullmatch(r"provider_http_[1-5][0-9]{2}", value) else "audit_read_failed"

def parse_item_ids(value):
    if not isinstance(value, str):
        raise ValueError("item IDs invalid")
    ids = value.split(",")
    if not 1 <= len(ids) <= 10 or len(set(ids)) != len(ids) or any(not re.fullmatch(r"[0-9]{1,32}", item) for item in ids):
        raise ValueError("item IDs invalid")
    return ids

def public_xml_id(value):
    try:
        return xml_id(value)
    except ValueError:
        return None

def feed_index(payload):
    root = avito_source_audit._root(payload, "Ads")
    index = defaultdict(list)
    candidates = []
    for ad in root:
        identifier = avito_source_audit._text(ad, "Id")
        raw_price = avito_source_audit._text(ad, "Price")
        try:
            amount = price(raw_price) if raw_price else None
        except ValueError:
            amount = None
        availability = avito_source_audit._text(ad, "Availability")
        record = {"xml_id": public_xml_id(identifier), "price": amount,
                  "availability": availability if availability in ("В наличии", "Под заказ") else None}
        if identifier:
            index[identifier].append(record)
        title = avito_source_audit._text(ad, "Title").casefold()
        if ("лагуна" in title or "laguna" in title) and re.search(r"(?<![\d.,])2(?:[.,]0)?(?![\d.,])", title) and re.search(r"(?<![\d.,])1[.,]25(?![\d.,])", title):
            if len(candidates) < 25:
                candidates.append(record)
    return index, {"sha256": hashlib.sha256(payload).hexdigest(), "xml_rows": len(root), "laguna_candidates": candidates}

def binding(connection):
    current = ChannelConnection.objects.select_related("channel", "avito_credential").get(pk=connection.pk)
    if not current.is_active or not current.channel.is_active or current.channel.kind != "avito":
        raise AvitoError("provider_account_mismatch")
    owners = OrganizationAccess.objects.filter(
        organization_id=current.channel.organization_id, user__is_active=True, role__in=("owner", "admin"),
    ).values_list("user_id", flat=True)
    if not CommunicationAccess.objects.filter(
        organization_id=current.channel.organization_id, user_id__in=owners, can_manage_channels=True,
    ).exists():
        raise AvitoError("provider_account_mismatch")
    try:
        credential = current.avito_credential
    except Exception:
        raise AvitoError("provider_credentials_missing") from None
    fingerprint = hashlib.sha256((credential.client_id_encrypted + "\0" + credential.client_secret_encrypted).encode()).hexdigest()
    return (current.channel_id, current.channel.organization_id, current.external_id, fingerprint)

def report_summary(report):
    samples = []
    rows = sorted(report["rows"], key=lambda row: not any(m["type"] == "error" for m in row["messages"]))
    groups = defaultdict(int)
    for row in rows:
        for key in {(m["type"], m["code"]) for m in row["messages"]}:
            groups[key] += 1
        if len(groups) > 200:
            raise AvitoError("audit_report_code_limit")
        critical = [message for message in row["messages"] if message["type"] in ("error", "alarm")]
        if critical and len(samples) < 25:
            samples.append({"xml_id": public_xml_id(row["ad_id"]),
                            "avito_id": str(row["avito_id"]) if row["avito_id"] is not None else None,
                            "messages": [{"type": m["type"], "code": m["code"], "title": safe_text(m["title"], 200),
                                          "description": safe_text(m["description"], 700)} for m in critical[:3]]})
    return {"upload_id": report["upload_id"], "upload_status": report["upload_status"],
            "complete": report["complete"], "finished": report["upload_finished"], "total": report["total"],
            "counts": report["counts"], "critical_samples": samples,
            "shop_feed_source": report.get("shop_feed_source", "other_or_unknown"),
            "started_at": datetime.fromisoformat(report["started_at"]).astimezone(ZoneInfo("Asia/Barnaul")).isoformat() if report.get("started_at") else None,
            "message_code_counts": [{"type": kind, "code": code, "rows": count}
                                    for (kind, code), count in sorted(groups.items())]}

def listing_summary(items, pages):
    # full_scan validates complete pagination and account identity. Titles are
    # used only locally; candidates expose a fingerprint and numeric IDs.
    counts = {status: 0 for status in avito_workspace.ITEM_STATUSES}
    by_title = defaultdict(list)
    for item_id, item in items.items():
        if (not isinstance(item_id, str) or not re.fullmatch(r"[0-9]{1,32}", item_id)
                or item["status"] not in counts or not isinstance(item["title"], str)):
            raise AvitoError("monitor_status_invalid")
        counts[item["status"]] += 1
        title = " ".join(item["title"].split()).casefold()
        if item["status"] == "active" and title:
            by_title[title].append(item_id)
    duplicates = [ids for ids in by_title.values() if len(ids) > 1]
    candidates = [{"title_sha256": hashlib.sha256(title.encode()).hexdigest(),
                   "active_count": len(ids), "item_ids": sorted(ids)[:10]}
                  for title, ids in by_title.items() if len(ids) > 1][:25]
    return {"complete": True, "total": len(items), "pages": pages, "counts": counts,
            "duplicate_title_group_count": len(duplicates), "duplicate_candidates": candidates}

def item_link(item_id, detail, index):
    source_id = detail["autoload_item_id"]
    matches = index.get(source_id, []) if index is not None and source_id else []
    if index is None:
        state = "xml_unavailable"
    elif not source_id:
        state = "no_autoload_id"
    elif len(matches) == 1:
        state = "exact_xml_match"
    elif len(matches) > 1:
        state = "xml_id_ambiguous"
    else:
        state = "not_in_current_xml"
    return {"item_id": item_id, "status": detail["status"], "autoload_xml_id": public_xml_id(source_id),
            "link_state": state, "xml_matches": matches[:10], "report_xml_ids": []}

def fetch_detail(token, account_id, item_id):
    deadline = time.monotonic() + 15
    while True:
        try:
            avito_workspace.enforce_rate(account_id, "item_detail", seconds=3)
            break
        except avito_workspace.AvitoCooldownError as error:
            wait = max(0.05, (error.retry_at - timezone.now()).total_seconds())
            if time.monotonic() + wait >= deadline:
                raise AvitoError("audit_provider_cooldown") from None
            time.sleep(min(wait, 3))
    return avito_workspace.fetch_item_detail(token, account_id, item_id)

def collect_connection(connection, item_ids, index, stocks=None, stock_error=None):
    identity = {"connection_id": connection.pk}
    try:
        before = binding(connection)
        # Existing .env/credentials remain on the host. Only the official root.
        if _provider_root() != "https://api.avito.ru":
            raise AvitoError("audit_provider_root_invalid")
        account_id = authorized_account_id(connection)
        if account_id != connection.external_id:
            raise AvitoError("provider_account_mismatch")
        token = access_token(connection)
        items = []
        for item_id in item_ids:
            try:
                items.append(item_link(item_id, fetch_detail(token, account_id, item_id), index))
            except Exception as error:
                items.append({"item_id": item_id, "error": safe_error(error)})
        report = None
        try:
            report = avito_autoload_report.fetch_report(token, account_id, kind="last_successful")
            summary = report_summary(report)
            for item in items:
                if "error" not in item:
                    item["report_xml_ids"] = [public_xml_id(row["ad_id"]) for row in report["rows"]
                                              if str(row["avito_id"]) == item["item_id"]][:10]
        except Exception as error:
            summary = {"error": safe_error(error)}
        try:
            all_items, pages = avito_status_monitor.full_scan(connection, scan_seconds=60)
            listing_scan = listing_summary(all_items, pages)
        except Exception as error:
            listing_scan = {"error": safe_error(error)}
        try:
            active_source = (avito_inventory.build_inventory(all_items, report, index, stocks, stock_error)
                             if "error" not in listing_scan else {"error": "audit_listing_scan_unavailable"})
        except Exception as error:
            active_source = {"error": safe_error(error)}
        final_account_id = authorized_account_id(connection)
        after = binding(connection)
        if before != after or final_account_id != account_id:
            raise AvitoError("provider_account_mismatch")
        return identity | {"account_id": account_id, "items": items, "autoload_report": summary, "listing_scan": listing_scan, "active_source_audit": active_source}
    except Exception as error:
        return identity | {"error": safe_error(error)}

def collect(item_ids):
    checked_at = datetime.now(ZoneInfo("Asia/Barnaul")).isoformat()
    if not isinstance(item_ids, list) or parse_item_ids(",".join(item_ids)) != item_ids:
        raise ValueError("item IDs invalid")
    connections = list(ChannelConnection.objects.filter(
        is_active=True, channel__kind="avito", channel__is_active=True, avito_credential__isnull=False,
    ).select_related("channel")[:MAX_CONNECTIONS + 1])
    if not connections or len(connections) > MAX_CONNECTIONS:
        return normalize({"schema_version": 1, "status": "blocked", "checked_at": checked_at,
                          "reason": "audit_connection_limit" if connections else "audit_no_active_connections",
                          "source": {"error": "source_unavailable"}, "connections": []})
    try:
        index, source = feed_index(avito_source_audit._read_xml(avito_source_audit.FEED_PATH))
    except Exception as error:
        index, source = None, {"error": safe_error(error)}
    try:
        stocks, _, _ = avito_source_audit.stock_values(avito_source_audit._read_xml(avito_source_audit.STOCK_PATH))
        stock_error = None
    except Exception as error:
        stocks, stock_error = None, safe_error(error)
    results = [collect_connection(connection, item_ids, index, stocks, stock_error) for connection in connections]
    complete = "error" not in source and all(
        "error" not in row and "error" not in row["autoload_report"] and "error" not in row["listing_scan"] and all("error" not in item for item in row["items"])
        for row in results
    )
    return normalize({"schema_version": 1, "status": "read" if complete else "partial",
                      "checked_at": checked_at, "source": source, "connections": results})
