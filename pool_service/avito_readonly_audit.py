"""Explicit hosting-operator diagnostics: reads Avito, never changes listings."""
import hashlib
import re
import time
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

from django.utils import timezone

from pool_service import avito_tariff_reminder, avito_autoload_auto
from pool_service.avito_feed_models import AvitoServicesFeed
from pool_service.models import Notification, Profile
from pool_service import avito_advance, avito_autoload_report, avito_source_audit, avito_workspace, avito_status_monitor, avito_inventory
from pool_service.avito_profile_source import fetch_profile_source
from pool_service.communication_avito import AvitoError, access_token, authorized_account_id, _provider_root
from pool_service.communication_models import ChannelConnection, CommunicationAccess
from pool_service.models import OrganizationAccess
from scripts.avito_readonly_receipt import normalize, safe_text, xml_id, price

MAX_CONNECTIONS = 2
SERVICES_CONNECTION_ID = 4
SERVICES_ACCOUNT_ID = "446312402"
SERVICES_REVISION = 15
SAFE_ERRORS = {
    "advance_data_invalid", "advance_provider_invalid", "provider_cooldown",
    "provider_credentials_missing", "provider_credentials_invalid", "provider_token_missing",
    "audit_provider_root_invalid", "audit_provider_cooldown", "audit_report_code_limit",
    "audit_profile_source_invalid", "audit_profile_source_changed",
    "monitor_scan_limit", "monitor_pagination_invalid", "monitor_status_invalid",
    "provider_account_id_missing", "provider_account_id_invalid", "provider_item_unavailable",
    "provider_account_mismatch", "provider_data_invalid", "provider_unavailable",
    "provider_response_too_large", "provider_invalid_json", "provider_invalid_response",
    "autoload_report_invalid", "autoload_report_changed", "autoload_report_limit", "autoload_report_pagination",
    "autoload_report_duplicate_rows", "audit_content_probe_unavailable",
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

def report_summary(report, connection=None):
    from .avito_report_messages import groups as message_groups
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
    from .avito_autoload_presentation import receipt_summary
    presentation = receipt_summary(connection, report) if connection is not None else None
    return ({"service_message_presentation": presentation} if presentation is not None else {}) | ({"input_copy_proof": report["input_copy_proof"]} if "input_copy_proof" in report else {}) | {"upload_id": report["upload_id"], "upload_status": report["upload_status"],
            "read_strategy": report.get("read_strategy", "provider_pages"),
            "complete": report["complete"], "finished": report["upload_finished"], "total": report["total"],
            "counts": report["counts"], "critical_samples": samples,
            "shop_feed_source": report.get("shop_feed_source", "other_or_unknown"),
            "shop_feed_diagnostics": report.get("shop_feed_diagnostics", []),
            "started_at": datetime.fromisoformat(report["started_at"]).astimezone(ZoneInfo("Asia/Barnaul")).isoformat() if report.get("started_at") else None,
            "message_code_counts": [{"type": kind, "code": code, "rows": count}
                                    for (kind, code), count in sorted(groups.items())],
            "message_groups": message_groups(rows)}

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

def _item_detail_slot(account_id):
    deadline = time.monotonic() + 15
    while True:
        try:
            avito_workspace.enforce_rate(account_id, "item_detail", seconds=3)
            return
        except avito_workspace.AvitoCooldownError as error:
            wait = max(0.05, (error.retry_at - timezone.now()).total_seconds())
            if time.monotonic() + wait >= deadline:
                raise AvitoError("audit_provider_cooldown") from None
            time.sleep(min(wait, 3))


def fetch_detail(token, account_id, item_id):
    _item_detail_slot(account_id)
    return avito_workspace.fetch_item_detail(token, account_id, item_id)


def _raw_field_shape(response, key):
    if key not in response:
        return {"present": False, "type": "missing", "count": None}
    value = response[key]
    kind = ("null" if value is None else "boolean" if type(value) is bool
            else "string" if isinstance(value, str) else "array" if isinstance(value, list)
            else "object" if isinstance(value, dict) else "number" if type(value) in (int, float)
            else None)
    if kind is None or kind in {"array", "object"} and len(value) > 1000:
        raise AvitoError("provider_data_invalid")
    return {"present": True, "type": kind, "count": len(value) if kind in {"array", "object"} else None}


def _published_probe_id(feed):
    published = [row for sheet in feed.published for row in sheet["rows"]]
    selected = next((row["AvitoId"] for row in published
                     if isinstance(row.get("AvitoId"), str)
                     and re.fullmatch(r"[0-9]{1,32}", row["AvitoId"])), None)
    if selected is None:
        raise AvitoError("audit_content_probe_unavailable")
    return selected


def raw_content_probe(token, account_id, feed):
    """One existing item endpoint; only fixed structural evidence leaves this function."""
    selected = _published_probe_id(feed)
    _item_detail_slot(account_id)
    # Existing transport bounds JSON to1MiB and timeout8s; no new provider endpoint.
    response = avito_workspace._get(token, f"/core/v1/accounts/{account_id}/items/{selected}/")
    status = response.get("status")
    if status in {"not_found", "another_user"}:
        raise AvitoError("provider_item_unavailable")
    if status not in avito_workspace.ITEM_STATUSES:
        raise AvitoError("provider_data_invalid")
    fields = {key: {"lowercase": _raw_field_shape(response, key),
                    "capitalized": _raw_field_shape(response, key.capitalize())}
              for key in ("title", "description", "images", "photos")}
    evidence = {"source": "avito_item_detail_raw_fields", "endpoint_kind": "core_v1_account_item",
        "checked_at": timezone.now().isoformat(), "owned_status_verified": True,
        "sample_count": 1, "live_content_verified": False, "fields": fields}
    # Reuse the same response for a requested link; raw values are not retained.
    detail = {"status": status, "autoload_item_id": avito_workspace._text(response.get("autoload_item_id"), 100)}
    return selected, detail, evidence

def monitoring_state(connection):
    """Configuration/evidence flags only: no amount, dates, recipient or message."""
    current = ChannelConnection.objects.select_related("channel").get(pk=connection.pk)
    if (current.channel_id != connection.channel_id
            or current.channel.organization_id != connection.channel.organization_id
            or current.external_id != connection.external_id):
        return {"error": "audit_monitoring_scope_changed"}
    advance = avito_advance._state(current)
    calendar = avito_tariff_reminder._state(current)
    advance_valid = bool(advance and avito_advance._allowed(current, advance))
    tariff_valid = bool(calendar and avito_tariff_reminder._allowed(current, calendar))
    def in_app(state, valid):
        if not valid:
            return None
        return Profile.objects.filter(user_id=state["actor_id"]).values_list(
            "in_app_notifications_enabled", flat=True).first() is not False
    recorded = None
    generation = advance.get("generation")
    if advance_valid and isinstance(generation, str) and re.fullmatch(r"[0-9a-f]{32}", generation):
        recorded = Notification.objects.filter(
            user_id=advance["actor_id"], organization_id=current.channel.organization_id,
            kind="communication",
            dedupe_key__startswith=f"avito-advance:{current.pk}:{generation}:",
        ).exists()
    return {
        "advance_configured": bool(advance), "advance_enabled": advance.get("enabled") is True,
        "advance_scope_valid": advance_valid,
        "advance_in_app_enabled": in_app(advance, advance_valid),
        "advance_notification_exists": recorded,
        "tariff_configured": bool(calendar), "tariff_enabled": calendar.get("enabled") is True,
        "tariff_scope_valid": tariff_valid, "tariff_in_app_enabled": in_app(calendar, tariff_valid),
        "amount_logged": False, "recipient_logged": False,
    }


def _legacy_photos_exceed_limit(sheets):
    """Inspect bounded saved snapshots without exporting or fetching image URLs."""
    if not isinstance(sheets, list) or len(sheets) > 200:
        return None
    over_limit, count = False, 0
    for sheet in sheets:
        if not isinstance(sheet, dict) or not isinstance(sheet.get("rows"), list):
            return None
        for row in sheet["rows"]:
            count += 1
            if count > 200 or not isinstance(row, dict):
                return None
            images = row.get("ImageUrls")
            if not isinstance(images, str) or len(images) > 32767:
                return None
            over_limit |= len([part for part in re.split(r"[|\r\n]+", images) if part.strip()]) > 10
    return over_limit


def cached_configuration_state(connection):
    """Local configuration only; cached webhook state has no immutable account binding."""
    current = ChannelConnection.objects.select_related("channel").get(pk=connection.pk)
    if (not current.is_active or not current.channel.is_active or current.channel.kind != "avito"
            or current.channel_id != connection.channel_id
            or current.channel.organization_id != connection.channel.organization_id
            or current.external_id != connection.external_id):
        return {"error": "audit_configuration_scope_changed"}
    metadata = current.settings if isinstance(current.settings, dict) else None
    auto = metadata.get(avito_autoload_auto.KEY, {}) if metadata is not None else None
    auto_valid = isinstance(auto, dict)
    try:
        auto_allowed = bool(auto and avito_autoload_auto._allowed(current, auto)) if auto_valid else None
    except (TypeError, ValueError):
        auto_allowed = None
    status = metadata.get("avito_webhook_status") if metadata is not None else None
    if not isinstance(status, str) or status not in {"connected", "not_connected", "needs_check", "error"}:
        status = "unknown"
    # Legacy status/received timestamps cannot prove which account/keys produced
    # them. Never turn them into positive registration or delivery evidence.
    result = {
        "source": "service2_cached_configuration", "live_provider_verified": False,
        "webhook_cache_present": ("avito_webhook_status" in metadata) if metadata is not None else None,
        "webhook_cache_status": status, "webhook_scope_known": False,
        "webhook_configured_cached": None, "webhook_verified_cached": None,
        "webhook_received_cached": None,
        "autoload_auto_configured": bool(auto) if auto_valid else None,
        "autoload_auto_enabled": (auto.get("enabled", False) if type(auto.get("enabled", False)) is bool else None) if auto_valid else None,
        "autoload_auto_scope_valid": auto_allowed,
        "services_feed_configured": False, "services_feed_account_bound": False,
        "services_feed_enabled": None, "services_feed_revision_current": None,
        "services_feed_legacy_photo_limit_exceeded": None,
    }
    feed = AvitoServicesFeed.objects.filter(connection=current).first()
    if feed is not None:
        bound = (feed.account_id == current.external_id
                 and feed.organization_id_at_import == current.channel.organization_id)
        result.update(services_feed_configured=True, services_feed_account_bound=bound)
        if bound:
            draft_photos, published_photos = (_legacy_photos_exceed_limit(feed.draft),
                                               _legacy_photos_exceed_limit(feed.published))
            result.update(
                services_feed_enabled=feed.enabled,
                services_feed_revision_current=bool(feed.published and feed.revision == feed.published_revision),
                services_feed_legacy_photo_limit_exceeded=(
                    draft_photos or published_photos
                    if draft_photos is not None and published_photos is not None else None),
            )
    return result



def services_item_link(item_id, detail, feed):
    rows = [row for sheet in feed.draft for row in sheet['rows']]
    row = next(row for row in rows if row['AvitoId'] == item_id)
    source_id = detail['autoload_item_id']
    state = ('services_feed_identity_only' if not source_id else
             'services_feed_match' if source_id == row['Id'] else 'services_feed_id_mismatch')
    return {'item_id': item_id, 'status': detail['status'], 'autoload_xml_id': public_xml_id(source_id),
            'link_state': state, 'xml_matches': [], 'report_xml_ids': []}

def services_coverage(feed, all_items, *, requested, checked, skipped):
    draft_ids = {row['AvitoId'] for sheet in feed.draft for row in sheet['rows']}
    published_ids = {row['AvitoId'] for sheet in feed.published for row in sheet['rows']}
    complete = isinstance(all_items, dict)
    active = {key for key, row in all_items.items() if row['status'] == 'active'} if complete else set()
    inactive = {key for key, row in all_items.items() if row['status'] != 'active'} if complete else set()
    return {'source': 'service2_services_feed', 'complete': complete,
        'draft_count': len(draft_ids), 'published_count': len(published_ids),
        'active_count': len(active) if complete else None,
        'active_covered': len(active & draft_ids) if complete else None,
        'active_missing': len(active - draft_ids) if complete else None,
        'published_inactive': len(published_ids & inactive) if complete else None,
        'requested_count': requested, 'checked_count': checked, 'skipped_out_of_scope': skipped}

def collect_connection(connection, item_ids, index, stocks=None, stock_error=None, *, release_evidence=False,
                       release_revision=SERVICES_REVISION):
    identity = {"connection_id": connection.pk}
    try:
        if release_evidence and (connection.pk != SERVICES_CONNECTION_ID
                                 or connection.external_id != SERVICES_ACCOUNT_ID):
            raise AvitoError("provider_account_mismatch")
        before = binding(connection)
        # Existing .env/credentials remain on the host. Only the official root.
        if _provider_root() != "https://api.avito.ru":
            raise AvitoError("audit_provider_root_invalid")
        account_id = authorized_account_id(connection)
        if account_id != connection.external_id:
            raise AvitoError("provider_account_mismatch")
        token = access_token(connection)
        try:
            configured_source, profile_fingerprint = fetch_profile_source(token, account_id)
        except Exception as error:
            configured_source, profile_fingerprint = {"error": safe_error(error)}, None
        try:
            avito_advance.fetch(token, account_id)
            advance_access = {"available": True, "source": "cpa_v3", "amount_logged": False}
        except Exception as error:
            advance_access = {"error": safe_error(error)}
        from .avito_input_copy import bound_services_feed
        service_feed = bound_services_feed(connection, account_id)
        if release_evidence and service_feed is None:
            raise AvitoError("provider_account_mismatch")
        service_ids = ({row['AvitoId'] for sheet in service_feed.draft for row in sheet['rows']}
                       if service_feed is not None else None)
        scoped_ids = [item_id for item_id in item_ids if service_ids is None or item_id in service_ids]
        raw_fields, probed_id, probed_detail = None, None, None
        if release_evidence:
            try:
                probed_id = _published_probe_id(service_feed)
                _, probed_detail, raw_fields = raw_content_probe(token, account_id, service_feed)
            except Exception as error:
                raw_fields = {"error": safe_error(error)}
        items = []
        for item_id in scoped_ids:
            try:
                if item_id == probed_id and probed_detail is None:
                    raise AvitoError(raw_fields["error"])
                detail = probed_detail if item_id == probed_id else fetch_detail(token, account_id, item_id)
                items.append(services_item_link(item_id, detail, service_feed) if service_feed is not None
                             else item_link(item_id, detail, index))
            except Exception as error:
                items.append({"item_id": item_id, "error": safe_error(error)})
        report = None
        try:
            from .avito_input_copy import services_input_scope
            proof_options = ({"include_input_proof": True, "input_proof_connection": connection}
                             if services_input_scope(connection, account_id) else {})
            if release_evidence:
                proof_options["allow_shop_recovery"] = False
            report = avito_autoload_report.fetch_report(token, account_id, kind="last_successful", **proof_options)
            summary = report_summary(report, connection)
            for item in items:
                if "error" not in item:
                    item["report_xml_ids"] = [public_xml_id(row["ad_id"]) for row in report["rows"]
                                              if str(row["avito_id"]) == item["item_id"]][:10]
        except Exception as error:
            summary = {"error": safe_error(error)}
            if isinstance(error, avito_autoload_report.ReportDuplicateRows):
                diagnostics = error.diagnostics.copy()
                if diagnostics.get("started_at"):
                    diagnostics["started_at"] = datetime.fromisoformat(
                        diagnostics["started_at"]
                    ).astimezone(ZoneInfo("Asia/Barnaul")).isoformat()
                summary["duplicate_diagnostics"] = diagnostics
        try:
            all_items, pages = avito_status_monitor.full_scan(connection, scan_seconds=60)
            listing_scan = listing_summary(all_items, pages)
        except Exception as error:
            listing_scan = {"error": safe_error(error)}
        if service_feed is not None:
            active_source = {"scope": "services", "state": "not_applicable",
                             "reason": "services_have_no_physical_stock"}
            service_coverage = services_coverage(service_feed, all_items if "error" not in listing_scan else None,
                requested=len(item_ids), checked=sum("error" not in row for row in items),
                skipped=len(item_ids) - len(scoped_ids))
        else:
            service_coverage = None
            try:
                active_source = (avito_inventory.build_inventory(all_items, report, index, stocks, stock_error)
                                 if "error" not in listing_scan else {"error": "audit_listing_scan_unavailable"})
            except Exception as error:
                active_source = {"error": safe_error(error)}
        if profile_fingerprint is not None:
            try:
                final_source, final_fingerprint = fetch_profile_source(token, account_id)
                if final_source != configured_source or final_fingerprint != profile_fingerprint:
                    raise AvitoError("audit_profile_source_changed")
            except Exception as error:
                configured_source = {"error": safe_error(error)}
        from .avito_services_workplace_setup import webhook_evidence, storage_probe_evidence
        try:
            webhook_state = webhook_evidence(connection, live=True)
        except Exception as error:
            webhook_state = {"error": safe_error(error)}
        try:
            storage_probe_state = storage_probe_evidence(connection)
        except Exception:
            # Independent local storage proof must survive a provider read failure,
            # and a storage diagnostic failure must not erase webhook evidence.
            storage_probe_state = {"source": "passenger_http", "checked": False, "cached": True, "error": "storage_probe_failed"}
        release_fields = _services_release_evidence(connection, revision=release_revision) if release_evidence else {}
        if release_evidence:
            release_fields["raw_content_fields"] = raw_fields
        final_account_id = authorized_account_id(connection)
        after = binding(connection)
        if before != after or final_account_id != account_id:
            raise AvitoError("provider_account_mismatch")
        try:
            flags = monitoring_state(connection)
        except Exception:
            flags = {"error": "audit_monitoring_unavailable"}
        try:
            cached_flags = cached_configuration_state(connection)
        except Exception:
            cached_flags = {"error": "audit_configuration_unavailable"}
        from .avito_runtime import checks
        runtime_checks = checks(ChannelConnection.objects.select_related("channel").get(pk=connection.pk))
        return identity | release_fields | ({"services_coverage": service_coverage} if service_coverage is not None else {}) | {"account_id": account_id, "items": items, "autoload_report": summary, "listing_scan": listing_scan, "active_source_audit": active_source,
                           "configured_feed_source": configured_source, "advance_access": advance_access,
                           "monitoring": flags, "cached_configuration": cached_flags, "runtime_checks": runtime_checks,
                           "webhook_evidence": webhook_state, "storage_probe": storage_probe_state}
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
        "error" not in row and "error" not in row["autoload_report"] and "error" not in row["listing_scan"] and "error" not in row["configured_feed_source"] and "error" not in row["advance_access"] and all("error" not in item for item in row["items"])
        for row in results
    )
    return normalize({"schema_version": 1, "status": "read" if complete else "partial",
                      "checked_at": checked_at, "source": source, "connections": results})


def _services_release_evidence(connection, *, revision=SERVICES_REVISION):
    """Independent read-only checks; no helper failure erases another result."""
    result = {}
    try:
        from .avito_services_publication import publication_release_check
        result["publication_release"] = publication_release_check(
            connection, revision=revision, check_http=True)
    except Exception:
        result["publication_release"] = {"error": "audit_publication_release_unavailable"}
    try:
        from .avito_webhook_chain import summary
        result["webhook_chain"] = summary(connection)
    except Exception:
        result["webhook_chain"] = {"error": "audit_webhook_chain_unavailable"}
    try:
        from .avito_services_media_livecheck import media_release_source
        result["approved_photo"] = media_release_source(connection)
    except Exception:
        result["approved_photo"] = {"error": "audit_approved_photo_unavailable"}
    try:
        from .avito_services_media_readiness import media_release_readiness
        result["media_readiness"] = media_release_readiness(connection)
    except Exception:
        result["media_readiness"] = {"error": "audit_media_readiness_unavailable"}
    try:
        from .avito_services_release_operation import release_operation_diagnostics
        result["operation_ledger"] = release_operation_diagnostics(connection)
    except Exception:
        result["operation_ledger"] = {"error": "audit_operation_ledger_unavailable"}
    try:
        from .avito_daily_statistics import source_period_evidence
        result["statistics_source_evidence"] = source_period_evidence(connection)
    except Exception:
        result["statistics_source_evidence"] = {"error": "audit_statistics_source_evidence_unavailable"}
    return result


def collect_services(item_ids=None, *, connection_id=SERVICES_CONNECTION_ID,
                     account_id=SERVICES_ACCOUNT_ID, revision=SERVICES_REVISION, current_publication=False):
    """Current services release only; never discover or read another account."""
    checked_at = datetime.now(ZoneInfo("Asia/Barnaul")).isoformat()
    from django.urls import reverse
    owner_page = reverse("avito_services_feed", args=[SERVICES_CONNECTION_ID])
    if owner_page != "/avito/4/services-feed/":
        raise ValueError("owner page invalid")
    envelope = {"schema_version": 1, "checked_at": checked_at, "prepared_owner_page": owner_page,
        "scope": {"kind": "services_readonly", "connection_id": SERVICES_CONNECTION_ID,
                  "account_id": SERVICES_ACCOUNT_ID, "revision": SERVICES_REVISION},
        "source": {"scope": "services", "state": "not_applicable",
                   "reason": "services_have_no_physical_stock"}, "connections": []}
    if (type(connection_id) is not int or connection_id != SERVICES_CONNECTION_ID
            or account_id != SERVICES_ACCOUNT_ID or type(revision) is not int
            or revision != SERVICES_REVISION or type(current_publication) is not bool):
        return normalize(envelope | {"status": "blocked", "reason": "audit_scope_invalid"})
    if item_ids is None:
        item_ids = []
    if not isinstance(item_ids, list) or (item_ids and parse_item_ids(",".join(item_ids)) != item_ids):
        raise ValueError("item IDs invalid")
    connection = ChannelConnection.objects.filter(
        pk=SERVICES_CONNECTION_ID, external_id=SERVICES_ACCOUNT_ID,
        is_active=True, channel__kind="avito", channel__is_active=True,
        avito_credential__isnull=False,
    ).select_related("channel").first()
    if connection is None:
        return normalize(envelope | {"status": "blocked", "reason": "audit_services_connection_unavailable"})
    # Fail before token/provider access if account/organization/feed/owner changed.
    try:
        selected_binding = binding(connection)
        from .avito_input_copy import bound_services_feed
        feed = bound_services_feed(connection, SERVICES_ACCOUNT_ID)
        if feed is None:
            raise AvitoError("provider_account_mismatch")
        if current_publication:
            selected = feed.published_revision
            selected_enabled = feed.enabled
            selected_photo_limit = feed.published_photo_limit
            if type(selected) is not int or not 0 < selected <= 1000000000 or not feed.enabled or not feed.published:
                raise AvitoError("provider_account_mismatch")
            revision = selected
            envelope["scope"].update(revision=selected, revision_source="current_published")
    except Exception:
        return normalize(envelope | {"status": "blocked", "reason": "audit_services_binding_invalid"})
    options = {"release_evidence": True}
    if current_publication:
        options["release_revision"] = revision
    row = collect_connection(connection, item_ids, None, **options)
    if current_publication:
        # A later publication/binding during provider I/O invalidates selection;
        # never label a historical version as the current result.
        try:
            current = ChannelConnection.objects.select_related("channel").get(pk=SERVICES_CONNECTION_ID)
            fresh = bound_services_feed(current, SERVICES_ACCOUNT_ID)
            if (binding(current) != selected_binding or fresh is None
                    or fresh.pk != feed.pk or fresh.published_revision != revision
                    or fresh.enabled is not True or fresh.enabled != selected_enabled
                    or fresh.published_photo_limit != selected_photo_limit
                    or fresh.public_key != feed.public_key or fresh.published != feed.published):
                raise ValueError
        except Exception:
            row = {"connection_id": SERVICES_CONNECTION_ID, "error": "audit_publication_changed"}
    complete = ("error" not in row and all(
        "error" not in row[key] for key in
        ("autoload_report", "listing_scan", "configured_feed_source", "advance_access",
         "publication_release", "webhook_chain", "approved_photo", "raw_content_fields",
         "media_readiness", "operation_ledger"))
        and all("error" not in item for item in row["items"]))
    return normalize(envelope | {"status": "read" if complete else "partial", "connections": [row]})
