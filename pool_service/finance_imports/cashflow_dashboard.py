"""Read-only detail helpers backed by canonical management cash-flow data."""

from pool_service.finance_imports.management_finance import (
    ZERO,
    cashflow_article_trend_source,
    management_cashflow_data,
)


MAX_ARTICLE_CHART_SERIES = 6
ARTICLE_MODE_ALL = "all"
ARTICLE_MODE_SELECTED = "selected"


def cashflow_dashboard_data(organization, first_month=None, last_month=None):
    """Compatibility facade for the canonical management cash-flow service."""
    return management_cashflow_data(organization, first_month, last_month)


def cashflow_article_trend_data(
    organization,
    first_month=None,
    last_month=None,
    *,
    mode=ARTICLE_MODE_ALL,
    selected_articles=(),
    cashflow_data=None,
):
    """Return article-by-month chart data without changing report totals."""
    source = cashflow_article_trend_source(
        organization,
        first_month,
        last_month,
        cashflow_data=cashflow_data,
    )
    months = source["months"]
    options = [dict(item) for item in source["options"]]
    available = {item["normalized_article_name"]: item["label"] for item in options}
    selected = []
    for value in selected_articles:
        if value in available and value not in selected:
            selected.append(value)

    mode = mode if mode in {ARTICLE_MODE_ALL, ARTICLE_MODE_SELECTED} else ARTICLE_MODE_ALL
    selection_error = ""
    requested_unique = list(dict.fromkeys(selected_articles))
    if mode == ARTICLE_MODE_SELECTED and len(requested_unique) > MAX_ARTICLE_CHART_SERIES:
        selection_error = (
            f"Можно одновременно показать не более {MAX_ARTICLE_CHART_SERIES} статей."
        )
        selected = []
    elif mode == ARTICLE_MODE_SELECTED and any(
        value not in available for value in requested_unique
    ):
        selection_error = "В выборе есть статья, недоступная за указанный период."
        selected = []

    for item in options:
        item["selected"] = item["normalized_article_name"] in selected

    datasets = []
    if months and not selection_error:
        if mode == ARTICLE_MODE_ALL:
            datasets.append({
                "key": ARTICLE_MODE_ALL,
                "label": "Все статьи",
                "values": [
                    item["net_cash_flow"] if item["has_data"] else None
                    for item in months
                ],
            })
        elif selected:
            for article in selected:
                datasets.append({
                    "key": article,
                    "label": available[article],
                    "values": [
                        item["article_net_cash_flow"].get(article, ZERO)
                        if item["has_data"] else None
                        for item in months
                    ],
                })

    return {
        "mode": mode,
        "options": options,
        "selected": [
            {"value": article, "label": available[article]}
            for article in selected
        ],
        "max_series": MAX_ARTICLE_CHART_SERIES,
        "labels": [item["period_month"].isoformat() for item in months],
        "datasets": datasets,
        "selection_error": selection_error,
        "has_active_months": source["has_active_months"],
    }
