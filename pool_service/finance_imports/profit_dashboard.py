from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

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
MONEY_QUANTUM = Decimal("0.01")
RATIO_QUANTUM = Decimal("0.0000000001")


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


def period_cost_ratio(rows):
    revenue = Decimal("0")
    cost = Decimal("0")
    excluded_sources = {
        OneCMonthlyProfit.COST_SOURCE_CALCULATED,
        OneCMonthlyProfit.COST_SOURCE_UNDEFINED,
    }
    for row in rows:
        if (
            classify_nomenclature_type(row.nomenclature_type) == "goods"
            and row.cost_source not in excluded_sources
            and row.cost is not None
            and row.cost != 0
            and row.revenue is not None
        ):
            revenue += row.revenue
            cost += row.cost
    if revenue == 0:
        return None
    return (cost / revenue).quantize(RATIO_QUANTUM, rounding=ROUND_HALF_UP)


def apply_period_analytics(rows):
    ratio = period_cost_ratio(rows)
    excluded_sources = {
        OneCMonthlyProfit.COST_SOURCE_CALCULATED,
        OneCMonthlyProfit.COST_SOURCE_UNDEFINED,
    }
    for row in rows:
        revenue = row.revenue or Decimal("0")
        is_goods = classify_nomenclature_type(row.nomenclature_type) == "goods"
        has_nonzero_actual_cost = (
            row.cost_source not in excluded_sources
            and row.cost is not None
            and row.cost != 0
        )
        use_period_ratio = is_goods and not has_nonzero_actual_cost and ratio is not None
        if use_period_ratio:
            analytical_cost = (revenue * ratio).quantize(
                MONEY_QUANTUM, rounding=ROUND_HALF_UP
            )
            gross_profit = (revenue - analytical_cost).quantize(
                MONEY_QUANTUM, rounding=ROUND_HALF_UP
            )
        elif is_goods and not has_nonzero_actual_cost:
            analytical_cost = None
            gross_profit = None
        else:
            analytical_cost = row.cost
            gross_profit = row.gross_profit if row.cost is not None else None

        row.dashboard_revenue = revenue
        row.dashboard_analytical_cost = analytical_cost
        row.dashboard_gross_profit = gross_profit
        row.dashboard_profitability = calculate_profitability(gross_profit, revenue)
        row.dashboard_cost_is_calculated = use_period_ratio
        row.dashboard_period_cost_ratio = ratio if use_period_ratio else None
    return ratio


def summarize(rows):
    revenue = Decimal("0")
    cost = Decimal("0")
    gross_profit = Decimal("0")
    for row in rows:
        revenue += row.dashboard_revenue
        cost += row.dashboard_analytical_cost or Decimal("0")
        gross_profit += row.dashboard_gross_profit or Decimal("0")
    return {
        "revenue": revenue, "cost": cost, "gross_profit": gross_profit,
        "profitability": calculate_profitability(gross_profit, revenue),
    }


def comparison(current, previous):
    result = {}
    for key in ("revenue", "cost", "gross_profit", "profitability"):
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
    current_ratio = apply_period_analytics(current_rows)
    previous_ratio = apply_period_analytics(previous_rows)
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
        "period_cost_ratio": current_ratio,
        "previous_period_cost_ratio": previous_ratio,
    }
