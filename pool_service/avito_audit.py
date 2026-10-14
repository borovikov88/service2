"""Read-only audit preparation; a partial inventory is never a successful report."""
import unicodedata
from collections import defaultdict

from pool_service import avito_status_monitor, avito_workspace

SCAN_SECONDS = 60
MAX_GROUPS = 25
MAX_GROUP_ITEMS = 10


def build_report(items, pages):
    """Summarize the validated full scan without saving provider payloads."""
    counts = {status: 0 for status in avito_workspace.ITEM_STATUSES}
    titles = defaultdict(list)
    for item in items.values():
        counts[item["status"]] += 1
        if item["status"] == "active" and item["title"].strip():
            title = unicodedata.normalize("NFKC", " ".join(item["title"].split())).casefold()
            titles[title].append(item)
    groups = [rows for _, rows in sorted(titles.items()) if len(rows) > 1]
    candidates = []
    for rows in groups[:MAX_GROUPS]:
        rows = sorted(rows, key=lambda item: int(item["id"]))
        candidates.append({
            "title": rows[0]["title"], "count": len(rows),
            "ids": [item["id"] for item in rows[:MAX_GROUP_ITEMS]],
            "omitted": max(0, len(rows) - MAX_GROUP_ITEMS),
        })
    return {
        "version": 1, "complete": True, "total": len(items), "pages": pages,
        "counts": [{"status": status, "label": label, "count": counts[status]}
                   for status, label in avito_workspace.ITEM_STATUSES.items()],
        "duplicate_group_count": len(groups),
        "duplicate_candidate_count": sum(len(rows) for rows in groups),
        "duplicate_groups": candidates,
        "duplicate_groups_omitted": max(0, len(groups) - MAX_GROUPS),
    }


def fetch_report(connection):
    # The web refresh lease lasts three minutes. A shorter scan budget leaves
    # room for authentication and the final HTTP request (15-second timeout).
    items, pages = avito_status_monitor.full_scan(connection, scan_seconds=SCAN_SECONDS)
    result = build_report(items, pages)
    result["active_source_audit"] = fetch_inventory(connection, items)
    return result


def fetch_inventory(connection, items):
    from pool_service import avito_inventory, avito_autoload_report
    from pool_service.communication_avito import access_token
    from pool_service.avito_readonly_audit import safe_error
    if not any(row["status"] == "active" for row in items.values()):
        return avito_inventory.build_inventory(items, {"complete": True, "rows": []}, {}, {})
    try:
        token = access_token(connection)
        upload = avito_autoload_report.fetch_report(token, str(connection.external_id), kind="last_successful")
        index, stocks, stock_error = avito_inventory.source_inputs()
        return avito_inventory.build_inventory(items, upload, index, stocks, stock_error)
    except Exception as error:
        return {"error": safe_error(error)}


def failure_detail(code):
    return {
        "monitor_scan_limit": "Обход не завершён в пределах времени. Полнота списка не подтверждена; предыдущий отчёт сохранён, если был.",
        "monitor_pagination_invalid": "Авито вернул неполную или противоречивую пагинацию. Полнота списка не подтверждена; предыдущий отчёт сохранён, если был.",
        "monitor_status_invalid": "Авито вернул неизвестный статус. Полнота списка не подтверждена; предыдущий отчёт сохранён, если был.",
    }.get(code)
