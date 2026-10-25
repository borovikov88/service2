"""Services-only report presentation; provider snapshots and export remain untouched."""
import json

from .avito_report_messages import _semantic_evidence, classify


def service_notice(message):
    """Only the known completed-binding warning is a service notice."""
    if (not isinstance(message, dict) or message.get("type") != "warning"
            or type(message.get("code")) is not int or message["code"] != 1218):
        return False
    semantics = _semantic_evidence(message)
    visible = classify(message)
    return bool(semantics["category"] == "existing_listing_link"
        and semantics["binding_evidence"] and not semantics["action_evidence"]
        and visible["category"] != "action_required" and not visible["action_evidence"])


def for_display(connection, report):
    """Return an independent view model, never a replacement provider report."""
    applicable = bool(getattr(connection, "pk", None) == 4
        and str(getattr(connection, "external_id", "")) == "446312402"
        and getattr(getattr(connection, "channel", None), "kind", None) == "avito"
        and isinstance(report, dict) and report.get("complete") is True)
    result = {"applicable": applicable, "counts": dict.fromkeys(
        ("error", "warning", "alarm", "info", "service_notice"), 0),
        "priority_rows": [], "service_rows": [], "other_rows": [], "has_attention": False}
    if not applicable:
        return result
    rows = report.get("rows")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        source = row.get("messages")
        messages = [dict(message, ui_service_notice=service_notice(message))
            for message in (source if isinstance(source, list) else []) if isinstance(message, dict)]
        rendered = {**row, "messages": messages}
        seen, priority, has_service = set(), False, False
        for message in messages:
            kind = message.get("type")
            if message["ui_service_notice"]:
                result["counts"]["service_notice"] += 1
                has_service = True
                continue
            if kind in {"error", "warning", "alarm", "info"}:
                seen.add(kind)
            semantic = _semantic_evidence(message)
            priority = priority or kind in {"error", "warning", "alarm"} or semantic["action_evidence"]
        for kind in seen:
            result["counts"][kind] += 1
        target = "priority_rows" if priority else "service_rows" if has_service else "other_rows"
        result[target].append(rendered)
    # Account-wide warning events have no per-listing binding evidence: retain them.
    events = report.get("events")
    event_attention = any(isinstance(event, dict) and event.get("type") in {"warning", "error", "alarm"}
        for event in (events if isinstance(events, list) else []))
    result["has_attention"] = bool(result["priority_rows"] or event_attention or report.get("upload_status") == "error")
    return result


def receipt_summary(connection, report):
    """Bounded counts from the same live report; no text, IDs of ads or I/O."""
    before = json.dumps(report, ensure_ascii=False, sort_keys=True)
    view = for_display(connection, report)
    if not view["applicable"]:
        return None
    rows = report.get("rows", [])
    counts = report.get("counts", {})
    return {
        "schema_version": 1, "connection_id": 4, "account_id": "446312402",
        "upload_id": report["upload_id"], "row_count": len(rows),
        "service_notice_messages": view["counts"]["service_notice"],
        "actionable_warning_rows": view["counts"]["warning"],
        "info_3002_messages": sum(message.get("type") == "info" and message.get("code") == 3002
            for row in rows for message in row.get("messages", []) if isinstance(message, dict)),
        "error_rows": view["counts"]["error"], "alarm_rows": view["counts"]["alarm"],
        "raw_warning_rows": counts.get("warning", 0), "raw_info_rows": counts.get("info", 0),
        "raw_snapshot_unchanged": before == json.dumps(report, ensure_ascii=False, sort_keys=True),
    }
