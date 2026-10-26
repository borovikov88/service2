"""Bounded typed reads of existing snapshots; never call the Avito provider."""
from __future__ import annotations
import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from django.core.exceptions import PermissionDenied
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from pool_service.avito_mcp_policy import ACCOUNT_ID, CONNECTION_ID, can_access_avito_mcp, scoped_connection

TOOL_NAMES = (
    "get_avito_funds", "get_avito_listings", "get_avito_upload_report",
    "get_avito_statistics", "get_avito_diagnostics",
)
CACHE_SCOPE_KEY = "assistant_read_binding_v1"
MAX_ROWS = 200


def _when(value):
    try:
        result = parse_datetime(value) if isinstance(value, str) else value
        return result.isoformat() if result and timezone.is_aware(result) and result <= timezone.now() else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def _count(value):
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _number(value):
    if type(value) not in (str, int, float, Decimal):
        return None
    try:
        result = Decimal(str(value))
        return format(result, "f") if result.is_finite() and abs(result) <= Decimal("1e15") else None
    except (InvalidOperation, ValueError):
        return None


def _identifier(value):
    return str(value) if type(value) in (str, int) and re.fullmatch(r"[1-9][0-9]{0,31}", str(value)) else None


def _code(value):
    return value if type(value) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", value) else None


def cache_binding(connection):
    from pool_service.avito_services_workplace_setup import _binding
    return _binding(connection) if connection.pk == CONNECTION_ID and str(connection.external_id) == ACCOUNT_ID else None


def _section(connection, name):
    metadata = connection.settings if type(connection.settings) is dict else {}
    workspace = metadata.get("avito_workspace")
    if type(workspace) is not dict or workspace.get("account_id") != ACCOUNT_ID:
        return {}, "unavailable"
    sections = workspace.get("sections")
    row = sections.get(name) if type(sections) is dict else None
    if type(row) is not dict:
        return {}, "unavailable"
    # A newly consented current-account token cannot adopt historical unbound data.
    stamp = row.get(CACHE_SCOPE_KEY)
    valid = type(stamp) is dict and stamp == cache_binding(connection)
    if not valid and name in {"advance", "autoload_report"}:
        from pool_service import avito_advance, avito_autoload_auto
        module = avito_advance if name == "advance" else avito_autoload_auto
        state = module._state(connection)
        success = _when(row.get("success_at"))
        valid = bool(module._allowed(connection, state) and success
            and success == _when(state.get("last_success_at")))
    if not valid:
        return {}, "cache_binding_unconfirmed"
    return row, "available"


def _part(row, source, data, *, status="available", complete=None):
    received = _when(row.get("success_at"))
    attempted = _when(row.get("checked_at"))
    return {"source": source, "received_at": received, "last_attempt_at": attempted,
            "state": status if received or status != "available" else "unavailable",
            "stale": bool(row.get("stale") is True or received and
                          timezone.now()-parse_datetime(received) > timedelta(hours=8)),
            "completeness": {"available": complete}, "data": data}


def _funds(connection, _actor, _arguments):
    from pool_service.avito_advance import for_display
    advance, advance_state = _section(connection, "advance")
    value = for_display(advance.get("data")) if advance else {}
    wallet, wallet_state = _section(connection, "balance")
    data = wallet.get("data") if type(wallet.get("data")) is dict else {}
    cash = _number(data.get("real")) if data.get("currency") == "RUB" else None
    bonus = _number(data.get("bonus")) if data.get("currency") == "RUB" else None
    return {"advance": _part(advance, "avito.cpa_v3.balanceInfo",
        {"amount": value.get("amount"), "currency": "RUB"}, status=advance_state,
        complete=bool(value) if advance else None),
        "wallet": _part(wallet, "avito.core_v1.accounts.balance",
        {"cash": cash, "bonuses": bonus, "currency": "RUB",
         "cash_known": cash is not None, "bonuses_known": bonus is not None},
        status=wallet_state, complete=cash is not None and bonus is not None if wallet else None),
        "funds_are_separate": True, "tariff_expiry_inferred": False}


def _feed(connection):
    from pool_service.avito_feed_models import AvitoServicesFeed
    return AvitoServicesFeed.objects.filter(connection=connection, account_id=ACCOUNT_ID,
        organization_id_at_import=connection.channel.organization_id).first()


def _listings(connection, _actor, _arguments):
    from pool_service.avito_services_feed import entries
    from pool_service.avito_placement_continuity import aware_time, source_calendar_date
    feed = _feed(connection)
    if feed is None:
        return {"state": "unavailable", "source": "avito_item_detail", "received_at": None,
                "completeness": {"all_listings": None}, "listings": []}
    owned_rows = entries(feed.draft)
    owned = {}
    identities_valid = len(owned_rows) <= MAX_ROWS
    for row in owned_rows:
        avito_id, xml_id = _identifier(row.get("AvitoId")), row.get("Id")
        if avito_id is None or type(xml_id) is not str or not xml_id or len(xml_id) > 128 or avito_id in owned:
            identities_valid = False
            continue
        owned[avito_id] = xml_id
    expected = set(owned) if identities_valid else set()
    expected_published = {_identifier(row.get("AvitoId")) for row in entries(feed.published)} - {None}
    observed = feed.latest_observations
    valid_rows = type(observed) is list and len(observed) <= MAX_ROWS
    rows, seen = [], set()
    for row in observed if valid_rows else []:
        if type(row) is not dict:
            continue
        identifier, when = _identifier(row.get("avito_id")), _when(row.get("checked_at"))
        status = row.get("status")
        if (identifier not in expected or identifier in seen or when is None
                or row.get("id") != owned.get(identifier)
                or row.get("source") != "avito_item_detail"
                or status not in {"active", "old", "removed", "blocked", "rejected"}):
            continue
        seen.add(identifier)
        instant = (aware_time(row.get("finish_time"))
                   if row.get("finish_time_source") == "avito_item_detail.finish_time" else None)
        finish = instant.isoformat() if instant is not None else None
        evidence = row.get("finish_evidence")
        evidence = evidence if type(evidence) is dict else {}
        calendar = (source_calendar_date(evidence.get("source_finish_date"))
            if row.get("finish_time") is None
            and evidence.get("source") == "avito_item_detail.finish_time"
            and evidence.get("finish_state") in {"timezone_missing", "calendar_date_only"}
            and evidence.get("finish_time") is None else None)
        rows.append({"avito_id": identifier, "status": status, "received_at": when,
            "source": "avito_item_detail", "finish_time": finish,
            "finish_time_source": "avito_item_detail.finish_time" if finish else None,
            "source_finish_date": calendar.isoformat() if calendar is not None else None,
            "source_timezone": "unknown" if calendar is not None else None,
            "deadline_known": finish is not None})
    return {"state": "available" if rows else "unavailable", "source": "avito_item_detail",
            "received_at": _when(feed.last_checked_at), "listings": rows,
            "completeness": {"expected_ids": sorted(expected), "expected_published_ids": sorted(expected_published),
                "observed_ids": sorted(seen),
                "all_listings": valid_rows and identities_valid and bool(expected) and seen == expected,
                "known_deadlines": sum(row["deadline_known"] for row in rows),
                "source_calendar_dates_without_timezone": sum(row["source_finish_date"] is not None for row in rows)},
            "reactivation_performed": False, "additional_cost_confirmed": False}


def _date_time_future(value):
    try:
        parsed = parse_datetime(value) if type(value) is str else None
        return parsed.isoformat() if parsed is not None and timezone.is_aware(parsed) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _upload(connection, _actor, _arguments):
    section, state = _section(connection, "autoload_report")
    report = section.get("data") if type(section.get("data")) is dict else {}
    rows = []
    raw = report.get("rows")
    for row in raw[:MAX_ROWS] if type(raw) is list else []:
        if type(row) is not dict:
            continue
        messages = row.get("messages")
        rows.append({"avito_id": _identifier(row.get("avito_id")),
            "status": _code(row.get("status")),
            "messages": [{"code": str(message.get("code")) if type(message.get("code")) in (int, str)
                         and re.fullmatch(r"[0-9]{1,12}", str(message.get("code"))) else None,
                         "level": message.get("type") if message.get("type") in {"info", "warning", "error", "alarm"} else None}
                        for message in messages[:20] if type(message) is dict] if type(messages) is list else []})
    feed = _feed(connection)
    publication = None
    if feed is not None:
        from pool_service.avito_publication_models import AvitoServicesPublication
        publication = AvitoServicesPublication.objects.filter(version__feed=feed,
            version__action="publish", version__published_revision=feed.published_revision,
            account_id=ACCOUNT_ID, organization_id=connection.channel.organization_id).select_related("version").order_by("-pk").first()
    attempt = publication.attempts.order_by("-checked_at", "-pk").first() if publication else None
    return {"report": _part(section, "avito.autoload_v4", {
        "upload_id": _identifier(report.get("upload_id")), "upload_status": _code(report.get("upload_status")),
        "upload_finished": report.get("upload_finished") is True, "started_at": _date_time_future(report.get("started_at")),
        "rows": rows}, status=state,
        complete=report.get("complete") is True and type(raw) is list and len(raw) <= MAX_ROWS),
        "proofs": {"file_published": publication is not None,
            "published_version": feed.published_revision if feed else None,
            "published_at": _when(publication.created_at) if publication else None,
            "subsequent_upload_observed": bool(attempt and attempt.report_started_at and
                                               attempt.report_started_at >= publication.created_at),
            "upload_id": attempt.upload_id if attempt else None,
            "source_version_confirmed": attempt.source_confirmed if attempt else None,
            "source_checked_at": _when(attempt.checked_at) if attempt else None,
            "source_state": _code(attempt.state) if attempt else None,
            "whole_card_content_verified": False,
            "partial_content_verification": "consult_field_evidence_history"},
        "provider_messages_text_omitted": True}


def _statistics(connection, actor, arguments):
    from pool_service import avito_daily_statistics, avito_period_statistics
    result = avito_period_statistics.display(connection, actor)
    start, end = arguments.get("date_from"), arguments.get("date_to")
    if (start is None) != (end is None):
        raise ValueError("period_requires_both_boundaries")
    if start is not None:
        for value in (start, end):
            if type(value) is not str or len(value) != 10:
                raise ValueError("period")
            date.fromisoformat(value)
        if start > end:
            raise ValueError("period")
    reports = []
    for report in result.get("reports", [])[:6]:
        if start is not None and (report["date_from"] != start or report["date_to"] != end):
            continue
        metric_keys = {str(metric["slug"]) for metric in report.get("metrics", []) if type(metric) is dict}
        rows = [{"avito_id": row["id"],
                 "metrics": {slug: _number(value) for slug, value in row["metrics"].items() if slug in avito_period_statistics.FIELDS}}
                for row in report["rows"]]
        reports.append({"source": result["source"], "date_from": report["date_from"],
            "date_to": report["date_to"], "grouping": report["grouping"],
            "received_at": _when(report["received_at"]), "provider_timezone": "unknown",
            "date_basis": "source_dates", "pages_complete": True, "ids": report["ids"],
            "rows": rows, "metrics": [{"slug": metric["slug"], "value": _number(metric["value"]),
                "unit": metric["unit"]} for metric in report["metrics"] if metric["slug"] in metric_keys],
            "completeness": {"pages_complete": True, "returned_ids": report["ids"],
                "known_metric_cells": sum(value is not None for row in rows for value in row["metrics"].values()),
                "requested_metric_cells": len(rows)*len(avito_period_statistics.FIELDS)}})
    history = avito_daily_statistics.source_period_evidence(connection)
    # History metadata is independent: period rows never fill missing daily facts.
    return {"state": result["state"] if reports or start is None else "requested_period_not_saved",
        "source": "own_ads_stats_v2_period", "provider_timezone": "unknown", "reports": reports,
        "completeness": {"saved_report_count": len(reports), "not_a_daily_reconstruction": True},
        "daily_history": {"source": "avito_stats_v2_items", "provider_timezone": "unknown",
            "completed_dates": _count(history.get("completed_dates")),
            "expected_dates": _count(history.get("expected_dates")),
            "state": _code(history.get("state")), "cutoff": history.get("cutoff") if type(history.get("cutoff")) is str else None,
            "received_at": _when(avito_daily_statistics._state(connection).get("last_success_at"))}}


def _diagnostics(connection, _actor, _arguments):
    from pool_service.avito_runtime import checks, safe_code
    data = []
    for row in checks(connection):
        data.append({"component": row["check"], "state": row["state"],
            "code": safe_code(row["code"], check=row["check"]),
            "received_at": _when(row["checked_at"]), "last_success_at": _when(row["success_at"]),
            "next_due_at": _date_time_future(row["next_due_at"]),
            "enabled": row["enabled"] is True, "scope_valid": row["scope_valid"],
            "last_worker_outcome": row["last_worker_outcome"],
            "last_worker_received_at": _when(row["last_worker_checked_at"])})
    return {"source": "service2.scoped_avito_runtime", "received_at": None,
        "components": data, "completeness": {"components": len(data)},
        "provider_refreshed": False}


def read_tool(actor, name, arguments):
    if name not in TOOL_NAMES or type(arguments) is not dict:
        raise ValueError("tool")
    allowed = {"connection_id", "date_from", "date_to"} if name == "get_avito_statistics" else {"connection_id"}
    if set(arguments)-allowed or type(arguments.get("connection_id")) is not int or arguments["connection_id"] != CONNECTION_ID:
        raise ValueError("scope")
    connection = scoped_connection()
    if connection is None or not can_access_avito_mcp(actor, connection.channel.organization):
        raise PermissionDenied
    binding = cache_binding(connection)
    callback = {"get_avito_funds": _funds, "get_avito_listings": _listings,
        "get_avito_upload_report": _upload, "get_avito_statistics": _statistics,
        "get_avito_diagnostics": _diagnostics}[name]
    data = callback(connection, actor, arguments)
    current = scoped_connection()
    if (current is None or cache_binding(current) != binding
            or not can_access_avito_mcp(actor, current.channel.organization)):
        raise PermissionDenied
    return {"connection_id": CONNECTION_ID, "avito_account_id": ACCOUNT_ID,
            "organization_id": connection.channel.organization_id,
            "source": "service2.saved_avito_workspace", "served_at": timezone.now().isoformat(),
            "provider_request_performed": False, "data": data}


def setup_evidence():
    """DB-only release receipt; no grants, tokens, sums, titles or customer fields."""
    from pool_service.avito_services_workplace_setup import _actor
    from pool_service.avito_mcp_auth import is_enabled, resource_url
    connection = scoped_connection()
    if connection is None:
        return {"state": "blocked", "connection_id": CONNECTION_ID, "code": "scope_unavailable"}
    try:
        actor = _actor(connection)
        summaries = []
        for name in TOOL_NAMES:
            result = read_tool(actor, name, {"connection_id": CONNECTION_ID})
            data = result["data"]
            summary = {"tool": name, "source": result["source"],
                       "provider_request_performed": False}
            if name == "get_avito_funds":
                summary["parts"] = [{"part": key, "state": data[key]["state"],
                    "source": data[key]["source"], "received_at": data[key]["received_at"],
                    "complete": data[key]["completeness"]["available"]} for key in ("advance", "wallet")]
            elif name == "get_avito_listings":
                summary.update(received_at=data["received_at"],
                    row_count=len(data["listings"]), completeness=data["completeness"])
            elif name == "get_avito_upload_report":
                summary.update(received_at=data["report"]["received_at"],
                    rows=len(data["report"]["data"]["rows"]),
                    complete=data["report"]["completeness"]["available"],
                    source_version_confirmed=data["proofs"]["source_version_confirmed"])
            elif name == "get_avito_statistics":
                summary.update(periods=[{"date_from": row["date_from"], "date_to": row["date_to"],
                    "received_at": row["received_at"], "rows": len(row["rows"]),
                    "pages_complete": row["pages_complete"]} for row in data["reports"]],
                    daily_history=data["daily_history"])
            else:
                summary["components"] = [{"component": row["component"], "state": row["state"],
                    "received_at": row["received_at"], "code": row["code"]} for row in data["components"]]
            summaries.append(summary)
        return {"state": "readers_checked", "connection_id": CONNECTION_ID,
                "account_id": ACCOUNT_ID, "enabled": is_enabled(), "resource": resource_url(),
                "scope": "service2.avito.read", "tools": summaries,
                "oauth_delivery_confirmed": False, "user_consent_required": True,
                "grants_created": False, "provider_request_performed": False}
    except Exception:
        return {"state": "blocked", "connection_id": CONNECTION_ID,
                "code": "reader_scope_or_cache_unavailable", "provider_request_performed": False}
