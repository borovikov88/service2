"""Owner UI adapter for the active finance-position snapshot."""

from pool_service.finance_imports.finance_position import (
    get_cash_position_breakdown,
    get_finance_position,
    get_settlement_position_breakdown,
)

CARD_SPECS = (
    ("cash_total", "Деньги всего"),
    ("receivables", "Нам должны"),
    ("supplier_advances", "Наши авансы поставщикам"),
    ("payables", "Мы должны поставщикам"),
    ("customer_advances", "Авансы клиентов"),
    ("calculated_position", "Расчётная позиция"),
)

CASH_LABELS = {
    "regular": "Счета и кассы",
    "kkm": "ККМ",
    "in_transit": "В пути",
}

SETTLEMENT_SECTIONS = (
    ("receivable", "Нам должны"),
    ("customer_advance", "Авансы клиентов"),
    ("payable", "Мы должны поставщикам"),
    ("supplier_advance", "Наши авансы поставщикам"),
)


def finance_position_dashboard_data(organization):
    summary = get_finance_position(organization)
    result = {
        "summary": summary,
        "cards": [
            {"key": key, "label": label, "value": summary.get(key)}
            for key, label in CARD_SPECS
        ],
        "cash_rows": [],
        "settlement_sections": [],
        "deleted_exclusions": {},
    }

    if not summary["available"]:
        return result

    for row in get_cash_position_breakdown(organization):
        item = dict(row)
        item["source_label"] = CASH_LABELS.get(
            item["source_kind"], item["source_kind"]
        )
        result["cash_rows"].append(item)

    for classification, label in SETTLEMENT_SECTIONS:
        breakdown = get_settlement_position_breakdown(
            organization,
            classification=classification,
            limit=50,
        )
        items = []
        for row in breakdown["items"]:
            item = dict(row)
            item["display_amount"] = abs(item["amount"])
            items.append(item)

        result["settlement_sections"].append({
            "classification": classification,
            "label": label,
            "count": breakdown["count"],
            "items": items,
        })

    result["deleted_exclusions"] = summary.get(
        "deleted_reference_exclusions", {}
    )

    return result
