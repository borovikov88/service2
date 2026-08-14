from decimal import Decimal

from django.db.models import Case, DecimalField, F, Sum, Value, When

from pool_service.finance_imports.services import calculate_profitability
from pool_service.models import OneCImportBatch, OneCMonthlyProfit


def _analytical_cost_expression():
    money = DecimalField(max_digits=20, decimal_places=2)
    return Case(
        When(
            cost_source=OneCMonthlyProfit.COST_SOURCE_CALCULATED,
            then=F("calculated_cost"),
        ),
        When(
            cost_source=OneCMonthlyProfit.COST_SOURCE_UNDEFINED,
            then=Value(None),
        ),
        default=F("cost"),
        output_field=money,
    )


def _analytical_profit_expression():
    return Case(
        When(cost_source="", then=F("gross_profit")),
        default=F("analytical_gross_profit"),
        output_field=DecimalField(max_digits=20, decimal_places=2),
    )


def get_gross_profit_dashboard(organization):
    """Build dashboard values from canonical active 1C report periods only."""
    rows = OneCMonthlyProfit.objects.active_for(
        organization,
        report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
    )
    cost = _analytical_cost_expression()
    profit = _analytical_profit_expression()
    totals = rows.aggregate(
        revenue=Sum("revenue"),
        cost=Sum(cost),
        gross_profit=Sum(profit),
    )
    for key in ("revenue", "cost", "gross_profit"):
        totals[key] = totals[key] or Decimal("0.00")
    totals["profitability_percent"] = calculate_profitability(
        totals["gross_profit"], totals["revenue"]
    )

    monthly = list(
        rows.order_by()
        .values("period_month")
        .annotate(
            revenue=Sum("revenue"),
            cost=Sum(cost),
            gross_profit=Sum(profit),
        )
        .order_by("period_month")
    )
    for item in monthly:
        item["revenue"] = item["revenue"] or Decimal("0.00")
        item["cost"] = item["cost"] or Decimal("0.00")
        item["gross_profit"] = item["gross_profit"] or Decimal("0.00")
        item["profitability_percent"] = calculate_profitability(
            item["gross_profit"], item["revenue"]
        )
    return {"totals": totals, "monthly": monthly}
