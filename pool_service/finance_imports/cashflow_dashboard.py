"""Read-only aggregation for the active cash-flow dataset."""

from decimal import Decimal

from django.db.models import Sum

from pool_service.models import CashFlowRow, OneCImportBatch


ZERO = Decimal("0.00")


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
