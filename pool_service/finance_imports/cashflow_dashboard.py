"""Read-only aggregation for the active cash-flow dataset."""

from datetime import date
from decimal import Decimal

from django.db.models import Min, Sum

from pool_service.models import (
    CashFlowRow,
    OneCImportBatch,
    OneCReportPeriodState,
)


ZERO = Decimal("0.00")
MAX_ARTICLE_CHART_SERIES = 6
ARTICLE_MODE_ALL = "all"
ARTICLE_MODE_SELECTED = "selected"


def _next_month(month):
    return date(
        month.year + (month.month == 12),
        1 if month.month == 12 else month.month + 1,
        1,
    )


def _month_sequence(first_month, last_month):
    if first_month is None or last_month is None or first_month > last_month:
        return []
    result = []
    current = first_month
    while current <= last_month:
        result.append(current)
        current = _next_month(current)
    return result


def cashflow_dashboard_data(organization, first_month=None, last_month=None):
    """Return the same cash-flow totals used by detail and overview screens."""
    rows = CashFlowRow.objects.active_for(
        organization, OneCImportBatch.TYPE_CASHFLOW
    )
    if first_month is not None:
        rows = rows.filter(period_month__gte=first_month)
    if last_month is not None:
        rows = rows.filter(period_month__lte=last_month)

    has_rows = rows.exists()
    totals = rows.aggregate(
        receipts=Sum("receipts"),
        payments=Sum("payments"),
        net_cash_flow=Sum("net_cash_flow"),
    )
    for key in ("receipts", "payments", "net_cash_flow"):
        totals[key] = totals[key] if totals[key] is not None else ZERO

    monthly = list(rows.values("period_month").annotate(
        receipts=Sum("receipts"),
        payments=Sum("payments"),
        net_cash_flow=Sum("net_cash_flow"),
    ).order_by("period_month"))
    articles = list(rows.values("article_raw").annotate(
        receipts=Sum("receipts"),
        payments=Sum("payments"),
        net_cash_flow=Sum("net_cash_flow"),
    ).order_by("article_raw"))
    return {
        "totals": totals,
        "monthly": monthly,
        "articles": articles,
        "has_rows": has_rows,
    }


def cashflow_article_trend_data(
    organization,
    first_month=None,
    last_month=None,
    *,
    mode=ARTICLE_MODE_ALL,
    selected_articles=(),
):
    """Return article-by-month chart data without changing report totals."""
    rows = CashFlowRow.objects.active_for(
        organization, OneCImportBatch.TYPE_CASHFLOW
    ).filter(import_batch__status=OneCImportBatch.STATUS_CONFIRMED)
    states = OneCReportPeriodState.objects.filter(
        organization=organization,
        report_type=OneCImportBatch.TYPE_CASHFLOW,
        active_batch__status=OneCImportBatch.STATUS_CONFIRMED,
        active_batch__import_type=OneCImportBatch.TYPE_CASHFLOW,
    )
    if first_month is not None:
        rows = rows.filter(period_month__gte=first_month)
        states = states.filter(period_month__gte=first_month)
    if last_month is not None:
        rows = rows.filter(period_month__lte=last_month)
        states = states.filter(period_month__lte=last_month)

    active_months = list(
        states.order_by("period_month").values_list("period_month", flat=True)
    )
    if first_month is not None and last_month is not None:
        months = _month_sequence(first_month, last_month)
    elif active_months:
        months = _month_sequence(active_months[0], active_months[-1])
    else:
        months = []
    active_month_set = set(active_months)

    options = list(
        rows.values("normalized_article_name")
        .annotate(label=Min("article_raw"))
        .order_by("label", "normalized_article_name")
    )
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
            totals = {
                item["period_month"]: item["net_cash_flow"]
                for item in rows.values("period_month").annotate(
                    net_cash_flow=Sum("net_cash_flow")
                )
            }
            datasets.append({
                "key": ARTICLE_MODE_ALL,
                "label": "Все статьи",
                "values": [
                    totals.get(month, ZERO) if month in active_month_set else None
                    for month in months
                ],
            })
        elif selected:
            values = {
                (item["normalized_article_name"], item["period_month"]): item["net_cash_flow"]
                for item in rows.filter(normalized_article_name__in=selected)
                .values("normalized_article_name", "period_month")
                .annotate(net_cash_flow=Sum("net_cash_flow"))
            }
            for article in selected:
                datasets.append({
                    "key": article,
                    "label": available[article],
                    "values": [
                        values.get((article, month), ZERO)
                        if month in active_month_set else None
                        for month in months
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
        "labels": [month.isoformat() for month in months],
        "datasets": datasets,
        "selection_error": selection_error,
        "has_active_months": bool(active_months),
    }
