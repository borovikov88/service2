"""Exact report/feed-ID candidates and saved shop quantities; no writes."""
from collections import defaultdict
from scripts.avito_readonly_receipt import inventory, INVENTORY_LABELS
from pool_service import avito_source_audit
from pool_service.communication_avito import AvitoError

def build_inventory(items, report, index, stocks=None, stock_error=None):
    if report is None or report.get("complete") is not True:
        return {"error": "audit_report_unavailable"}
    if index is None:
        return {"error": "source_unavailable"}
    active = sorted((str(key) for key, row in items.items() if row["status"] == "active"), key=int)
    if len(active) > 500:
        return {"error": "audit_inventory_limit"}
    by_item = defaultdict(list)
    by_xml = defaultdict(list)
    for row in report["rows"]:
        by_xml[row["ad_id"]].append(row["avito_id"])
        if row["avito_id"] is not None:
            by_item[str(row["avito_id"])].append(row["ad_id"])
    rows = []
    for item_id in active:
        bindings = by_item.get(item_id, [])
        matches = index.get(bindings[0], []) if len(bindings) == 1 else []
        state = ("not_in_last_report" if not bindings else "ambiguous" if len(bindings) != 1 or len(by_xml[bindings[0]]) != 1
                 else "not_in_xml" if not matches else "ambiguous" if len(matches) != 1
                 else "invalid_xml_id" if matches[0]["xml_id"] is None else "report_xml_match")
        record = matches[0] if state == "report_xml_match" else {}
        quantity = (stocks or {}).get(bindings[0]) if state == "report_xml_match" else None
        rows.append({"item_id": item_id, "link_state": state, "xml_id": record.get("xml_id"),
                     "availability": record.get("availability"), "shop_quantity": quantity})
    result = {"complete": True, "total": len(rows), "rows": rows,
              "counts": {key: sum(row["link_state"] == key for row in rows) for key in INVENTORY_LABELS},
              "quantity_verified_rows": sum(row["shop_quantity"] is not None for row in rows),
              "nonpositive_shop_quantity_rows": sum(row["shop_quantity"] is not None and row["shop_quantity"] <= 0 for row in rows),
              "in_stock_nonpositive_rows": sum(row["shop_quantity"] is not None and row["shop_quantity"] <= 0
                                               and row["availability"] == "В наличии" for row in rows),
              "preorder_rows": sum(row["availability"] == "Под заказ" for row in rows)}
    if stock_error:
        result["stock_error"] = stock_error
    return inventory(result)

def source_inputs():
    # Import at call time to avoid workspace/audit module initialization cycles.
    from pool_service.avito_readonly_audit import feed_index, safe_error
    index, _ = feed_index(avito_source_audit._read_xml(avito_source_audit.FEED_PATH))
    try:
        stocks, _, _ = avito_source_audit.stock_values(
            avito_source_audit._read_xml(avito_source_audit.STOCK_PATH))
        return index, stocks, None
    except AvitoError as error:
        return index, None, safe_error(error)
