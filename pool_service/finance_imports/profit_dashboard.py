from calendar import monthrange
from datetime import date
from decimal import Decimal

from django.utils import timezone

from pool_service.finance_imports.monthly_profit_parser import classify_nomenclature_type
from pool_service.finance_imports.services import calculate_profitability
from pool_service.models import OneCMonthlyProfit


PERIOD_CHOICES = (
    ("current_month", "Текущий месяц"),
    ("previous_month", "Прошлый месяц"),
    ("current_year", "Текущий год"),
    ("previous_year", "Прошлый год"),
    ("custom", "Произвольный период"),
)


def add_months(value, offset):
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def month_end(value):
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def resolve_period(params, today=None):
    today = today or timezone.localdate()
    preset = params.get("period", "")
    if preset == "current_month":
        start, end = today.replace(day=1), today
    elif preset == "previous_month":
        start = add_months(today.replace(day=1), -1)
        end = month_end(start)
    elif preset == "current_year":
        start, end = date(today.year, 1, 1), today
    elif preset == "previous_year":
        start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif preset == "custom":
        try:
            start = date.fromisoformat(params.get("start", ""))
            end = date.fromisoformat(params.get("end", ""))
            if start > end:
                raise ValueError
        except ValueError:
            start, end, preset = date(today.year, 1, 1), today, ""
    else:
        start, end, preset = date(today.year, 1, 1), today, ""
    first_month = start.replace(day=1)
    last_month = end.replace(day=1)
    month_count = (last_month.year - first_month.year) * 12 + last_month.month - first_month.month + 1
    previous_first = add_months(first_month, -month_count)
    previous_last = add_months(first_month, -1)
    return {
        "preset": preset, "start": start, "end": end,
        "first_month": first_month, "last_month": last_month,
        "previous_first": previous_first, "previous_last": previous_last,
    }


def effective_values(row):
    if row.cost_source == OneCMonthlyProfit.COST_SOURCE_CALCULATED:
        cost, profit = row.calculated_cost, row.analytical_gross_profit
    elif row.cost_source == OneCMonthlyProfit.COST_SOURCE_UNDEFINED:
        cost, profit = None, None
    else:
        cost, profit = row.cost, row.gross_profit
    return row.revenue or Decimal("0"), cost, profit


def summarize(rows):
    revenue = Decimal("0")
    cost = Decimal("0")
    gross_profit = Decimal("0")
    for row in rows:
        row_revenue, row_cost, row_profit = effective_values(row)
        revenue += row_revenue
        cost += row_cost or Decimal("0")
        gross_profit += row_profit or Decimal("0")
    return {
        "revenue": revenue, "cost": cost, "gross_profit": gross_profit,
        "profitability": calculate_profitability(gross_profit, revenue),
    }


def comparison(current, previous):
    result = {}
    for key in ("revenue", "gross_profit", "profitability"):
        value = current[key]
        old = previous[key]
        absolute = None if value is None or old is None else value - old
        percent = None
        if absolute is not None and old != 0:
            percent = (absolute * Decimal("100") / abs(old)).quantize(Decimal("0.01"))
        result[key] = {"absolute": absolute, "percent": percent}
    return result


def dashboard_data(organization, period):
    all_rows = OneCMonthlyProfit.objects.active_for(organization).filter(
        period_month__range=(period["previous_first"], period["last_month"])
    )
    current_rows = list(all_rows.filter(
        period_month__range=(period["first_month"], period["last_month"])
    ))
    previous_rows = list(all_rows.filter(
        period_month__range=(period["previous_first"], period["previous_last"])
    ))
    monthly = []
    for month_index in range(
        (period["last_month"].year - period["first_month"].year) * 12
        + period["last_month"].month - period["first_month"].month + 1
    ):
        month = add_months(period["first_month"], month_index)
        totals = summarize(row for row in current_rows if row.period_month == month)
        monthly.append({"month": month, **totals})
    split = []
    for kind, label in (("goods", "Товары"), ("service", "Работы и услуги")):
        split.append({
            "kind": kind, "label": label,
            **summarize(row for row in current_rows if classify_nomenclature_type(row.nomenclature_type) == kind),
        })
    current = summarize(current_rows)
    previous = summarize(previous_rows)
    return {
        "rows": current_rows, "totals": current, "previous_totals": previous,
        "comparison": comparison(current, previous), "monthly": monthly, "split": split,
    }
