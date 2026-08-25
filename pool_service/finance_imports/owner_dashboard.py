"""Read-only owner dashboard composed from canonical active report services."""

from calendar import monthrange
from datetime import date
from decimal import Decimal
import re
from urllib.parse import urlencode

from django.db.models import Max
from django.utils import timezone

from pool_service.finance_imports.cashflow_dashboard import cashflow_dashboard_data
from pool_service.finance_imports.cost_control import (
    get_onec_cost_anomalies,
    summarize_cost_anomalies,
)
from pool_service.finance_imports.payroll_dashboard import payroll_dashboard_data
from pool_service.finance_imports.profit_dashboard import (
    MONTH_SHORT_NAMES,
    add_months,
    comparison_period_label,
    monthly_profit_summary,
)
from pool_service.models import OneCImportBatch, OneCReportPeriodState


ZERO = Decimal("0.00")
PERCENT_QUANTUM = Decimal("0.01")
PERIOD_CHOICES = (
    ("current_month", "Текущий месяц"),
    ("previous_month", "Предыдущий месяц"),
    ("current_year", "Текущий год"),
    ("previous_year", "Предыдущий год"),
    ("last_12_months", "Последние 12 месяцев"),
    ("off_season", "Несезон · ноябрь–март"),
    ("custom", "Произвольный период"),
)
SOURCE_DEFINITIONS = {
    "profit": {
        "label": "Валовая прибыль",
        "report_type": OneCImportBatch.TYPE_MONTHLY_PROFIT,
        "route_name": "finance_onec_profit_dashboard",
    },
    "payroll": {
        "label": "ФОТ",
        "report_type": OneCImportBatch.TYPE_PAYROLL,
        "route_name": "finance_payroll_dashboard",
    },
    "cashflow": {
        "label": "ДДС",
        "report_type": OneCImportBatch.TYPE_CASHFLOW,
        "route_name": "finance_onec_cashflow_dashboard",
    },
}


def month_end(value):
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def month_sequence(first, last):
    result = []
    current = first
    while current <= last:
        result.append(current)
        current = add_months(current, 1)
    return result


def _parse_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value):
        raise ValueError
    parsed = date.fromisoformat(f"{value}-01" if len(value) == 7 else value)
    return parsed.replace(day=1)


def _off_season(today):
    current = today.replace(day=1)
    if today.month >= 11:
        return date(today.year, 11, 1), current
    if today.month <= 3:
        return date(today.year - 1, 11, 1), current
    return date(today.year - 1, 11, 1), date(today.year, 3, 1)


def resolve_owner_period(params, *, today=None):
    """Resolve only full calendar months for the owner dashboard."""
    today = today or timezone.localdate()
    current_month = today.replace(day=1)
    preset = params.get("period") or "current_month"
    error = ""
    if preset == "current_month":
        first = last = current_month
    elif preset == "previous_month":
        first = last = add_months(current_month, -1)
    elif preset == "current_year":
        first, last = date(today.year, 1, 1), current_month
    elif preset == "previous_year":
        first, last = date(today.year - 1, 1, 1), date(today.year - 1, 12, 1)
    elif preset == "last_12_months":
        first, last = add_months(current_month, -11), current_month
    elif preset == "off_season":
        first, last = _off_season(today)
    elif preset == "custom":
        try:
            first = _parse_month(params.get("start", ""))
            last = _parse_month(params.get("end", ""))
            if first > last:
                raise ValueError
        except ValueError:
            first = last = current_month
            error = "Укажите корректный диапазон полных месяцев."
    else:
        preset = "current_month"
        first = last = current_month
        error = "Неизвестный период заменён текущим месяцем."
    return {
        "preset": preset,
        "first_month": first,
        "last_month": last,
        "start": first,
        "end": month_end(last),
        "error": error,
        "months": month_sequence(first, last),
        "is_preliminary": first <= current_month <= last,
    }


def _comparison_specs(period):
    first = period["first_month"]
    last = period["last_month"]
    if first == last:
        previous_month = add_months(first, -1)
        previous_year = add_months(first, -12)
        return [
            {
                "key": "previous_month",
                "first": previous_month,
                "last": previous_month,
                "label": f"К предыдущему месяцу · {comparison_period_label(previous_month, previous_month)}",
            },
            {
                "key": "previous_year",
                "first": previous_year,
                "last": previous_year,
                "label": (
                    "К тому же месяцу прошлого года · "
                    f"{comparison_period_label(previous_year, previous_year)}"
                ),
            },
        ]
    previous_first = add_months(first, -12)
    previous_last = add_months(last, -12)
    prefix = "К предыдущему несезону" if period["preset"] == "off_season" else "К аналогичному периоду"
    return [{
        "key": "previous_year",
        "first": previous_first,
        "last": previous_last,
        "label": f"{prefix} · {comparison_period_label(previous_first, previous_last)}",
    }]


def _source_state(organization, source_key, months, *, include_freshness):
    definition = SOURCE_DEFINITIONS[source_key]
    states = {
        item.period_month: item
        for item in OneCReportPeriodState.objects.filter(
            organization=organization,
            report_type=definition["report_type"],
            period_month__in=months,
            active_batch__status=OneCImportBatch.STATUS_CONFIRMED,
            active_batch__import_type=definition["report_type"],
        ).select_related("active_batch")
    }
    missing = [month for month in months if month not in states]
    freshness = {"data_through": None, "last_updated": None}
    if include_freshness:
        freshness = OneCReportPeriodState.objects.filter(
            organization=organization,
            report_type=definition["report_type"],
            active_batch__status=OneCImportBatch.STATUS_CONFIRMED,
            active_batch__import_type=definition["report_type"],
        ).aggregate(data_through=Max("period_month"), last_updated=Max("updated_at"))
    return {
        **definition,
        "states": states,
        "missing_months": missing,
        "has_any": bool(states),
        "complete": not missing,
        **freshness,
    }


def _ratio(numerator, denominator):
    if numerator is None or denominator in (None, ZERO):
        return None
    return (numerator * Decimal("100") / denominator).quantize(PERCENT_QUANTUM)


def _range_data(organization, first, last, *, include_freshness=True):
    months = month_sequence(first, last)
    sources = {
        key: _source_state(
            organization, key, months, include_freshness=include_freshness
        )
        for key in SOURCE_DEFINITIONS
    }
    profit = monthly_profit_summary(organization, first, last)
    payroll = payroll_dashboard_data(organization, first, last)
    cashflow = cashflow_dashboard_data(organization, first, last)

    profit_values = profit["totals"] if sources["profit"]["has_any"] else None
    payroll_value = payroll["accrued"] if sources["payroll"]["has_any"] else None
    cash_values = cashflow["totals"] if sources["cashflow"]["has_any"] else None
    values = {
        "revenue": profit_values["revenue"] if profit_values else None,
        "gross_profit": profit_values["gross_profit"] if profit_values else None,
        "gross_margin": profit_values["profitability"] if profit_values else None,
        "payroll": payroll_value,
        "receipts": cash_values["receipts"] if cash_values else None,
        "payments": cash_values["payments"] if cash_values else None,
        "net_cash_flow": cash_values["net_cash_flow"] if cash_values else None,
    }
    if sources["profit"]["complete"] and sources["payroll"]["complete"]:
        values["payroll_to_gross_profit"] = _ratio(payroll_value, values["gross_profit"])
        values["payroll_to_revenue"] = _ratio(payroll_value, values["revenue"])
    else:
        values["payroll_to_gross_profit"] = None
        values["payroll_to_revenue"] = None

    profit_monthly = {item["month"]: item for item in profit["monthly"]}
    payroll_monthly = {item["period_month"]: item for item in payroll["months"]}
    cash_monthly = {item["period_month"]: item for item in cashflow["monthly"]}
    monthly = []
    for month in months:
        profit_item = profit_monthly.get(month, {})
        payroll_item = payroll_monthly.get(month, {})
        cash_item = cash_monthly.get(month, {})
        monthly.append({
            "month": month,
            "label": f"{MONTH_SHORT_NAMES[month.month]} {month.year}",
            "revenue": (
                profit_item.get("revenue", ZERO)
                if month in sources["profit"]["states"] else None
            ),
            "gross_profit": (
                profit_item.get("gross_profit", ZERO)
                if month in sources["profit"]["states"] else None
            ),
            "payroll": (
                payroll_item.get("accrued", ZERO)
                if month in sources["payroll"]["states"] else None
            ),
            "receipts": (
                cash_item.get("receipts", ZERO)
                if month in sources["cashflow"]["states"] else None
            ),
            "payments": (
                cash_item.get("payments", ZERO)
                if month in sources["cashflow"]["states"] else None
            ),
            "net_cash_flow": (
                cash_item.get("net_cash_flow", ZERO)
                if month in sources["cashflow"]["states"] else None
            ),
        })
    return {
        "first_month": first,
        "last_month": last,
        "months": months,
        "sources": sources,
        "values": values,
        "monthly": monthly,
        "raw": {"profit": profit, "payroll": payroll, "cashflow": cashflow},
    }


def _change(current, previous):
    absolute = current - previous
    percent = None
    if previous != 0:
        percent = (absolute * Decimal("100") / abs(previous)).quantize(PERCENT_QUANTUM)
    return absolute, percent


def _tone(absolute, polarity, *, preliminary):
    if preliminary or absolute == 0:
        return "neutral"
    if polarity == "higher":
        return "positive" if absolute > 0 else "negative"
    if polarity == "lower":
        return "negative" if absolute > 0 else "positive"
    if polarity == "payments":
        return "negative" if absolute > 0 else "neutral"
    return "neutral"


def _detail_query(route_name, period):
    first = period["first_month"].strftime("%Y-%m")
    last = period["last_month"].strftime("%Y-%m")
    if route_name == "finance_onec_profit_dashboard":
        return urlencode({"period": "custom", "start": first, "end": last})
    if route_name in {"finance_payroll_dashboard", "finance_onec_cashflow_dashboard"}:
        return urlencode({"period_from": first, "period_to": last})
    return ""


def _card(definition, current, comparisons, period):
    source_keys = definition["source_keys"]
    value = current["values"][definition["key"]]
    current_complete = all(current["sources"][key]["complete"] for key in source_keys)
    comparison_items = []
    for spec, comparison_data in comparisons:
        comparison_complete = (
            current_complete
            and all(comparison_data["sources"][key]["complete"] for key in source_keys)
        )
        previous = comparison_data["values"][definition["key"]]
        if not comparison_complete or value is None or previous is None:
            comparison_items.append({
                "key": spec["key"],
                "available": False,
                "label": "Нет полного сопоставимого периода",
                "absolute": None,
                "percent": None,
                "tone": "neutral",
            })
            continue
        absolute, percent = _change(value, previous)
        comparison_items.append({
            "key": spec["key"],
            "available": True,
            "label": spec["label"],
            "absolute": absolute,
            "percent": percent,
            "tone": _tone(
                absolute, definition["polarity"],
                preliminary=period["is_preliminary"],
            ),
        })
    route_name = definition["route_name"]
    return {
        **definition,
        "value": value,
        "has_data": value is not None,
        "partial": not current_complete and any(
            current["sources"][key]["has_any"] for key in source_keys
        ),
        "preliminary": period["is_preliminary"],
        "comparisons": comparison_items,
        "freshness": [current["sources"][key] for key in source_keys],
        "query": _detail_query(route_name, period),
    }


ECONOMY_CARDS = (
    {"key": "revenue", "label": "Выручка", "value_type": "money", "absolute_type": "money", "source_keys": ("profit",), "route_name": "finance_onec_profit_dashboard", "polarity": "higher"},
    {"key": "gross_profit", "label": "Валовая прибыль", "value_type": "money", "absolute_type": "money", "source_keys": ("profit",), "route_name": "finance_onec_profit_dashboard", "polarity": "higher"},
    {"key": "gross_margin", "label": "Валовая рентабельность", "value_type": "percent", "absolute_type": "points", "source_keys": ("profit",), "route_name": "finance_onec_profit_dashboard", "polarity": "higher"},
    {"key": "payroll", "label": "Начисленный ФОТ", "value_type": "money", "absolute_type": "money", "source_keys": ("payroll",), "route_name": "finance_payroll_dashboard", "polarity": "lower"},
    {"key": "payroll_to_gross_profit", "label": "ФОТ / валовая прибыль", "value_type": "percent", "absolute_type": "points", "source_keys": ("profit", "payroll"), "route_name": "finance_payroll_dashboard", "polarity": "lower"},
    {"key": "payroll_to_revenue", "label": "ФОТ / выручка", "value_type": "percent", "absolute_type": "points", "source_keys": ("profit", "payroll"), "route_name": "finance_payroll_dashboard", "polarity": "lower"},
)
CASHFLOW_CARDS = (
    {"key": "receipts", "label": "Поступления", "value_type": "money", "absolute_type": "money", "source_keys": ("cashflow",), "route_name": "finance_onec_cashflow_dashboard", "polarity": "higher"},
    {"key": "payments", "label": "Платежи", "value_type": "money", "absolute_type": "money", "source_keys": ("cashflow",), "route_name": "finance_onec_cashflow_dashboard", "polarity": "payments"},
    {"key": "net_cash_flow", "label": "Чистый денежный поток", "value_type": "money", "absolute_type": "money", "source_keys": ("cashflow",), "route_name": "finance_onec_cashflow_dashboard", "polarity": "higher"},
)


def _seasonality(organization, today):
    current_first = date(today.year, 1, 1)
    current_last = today.replace(day=1)
    previous_first = date(today.year - 1, 1, 1)
    previous_last = date(today.year - 1, today.month, 1)
    current = _range_data(
        organization, current_first, current_last, include_freshness=False
    )
    previous = _range_data(
        organization, previous_first, previous_last, include_freshness=False
    )
    result = {}
    definitions = {
        "gross_profit": ("Валовая прибыль", ("profit",)),
        "payroll": ("ФОТ", ("payroll",)),
        "net_cash_flow": ("Чистый денежный поток", ("cashflow",)),
    }
    for metric, (label, source_keys) in definitions.items():
        available = all(
            current["sources"][key]["complete"]
            and previous["sources"][key]["complete"]
            for key in source_keys
        )
        rows = []
        if available:
            for current_row, previous_row in zip(current["monthly"], previous["monthly"]):
                rows.append({
                    "month_number": current_row["month"].month,
                    "label": MONTH_SHORT_NAMES[current_row["month"].month],
                    "current": current_row[metric],
                    "previous": previous_row[metric],
                })
        result[metric] = {
            "label": label,
            "available": available,
            "rows": rows,
            "current_year": today.year,
            "previous_year": today.year - 1,
        }
    return result


def _signals(current, period, anomaly_summary):
    signals = []
    if (
        current["sources"]["cashflow"]["complete"]
        and current["values"]["net_cash_flow"] is not None
        and current["values"]["net_cash_flow"] < 0
    ):
        signals.append({
            "kind": "negative",
            "title": "Отрицательный чистый денежный поток",
            "detail": current["values"]["net_cash_flow"],
            "detail_type": "money",
            "route_name": "finance_onec_cashflow_dashboard",
            "query": _detail_query("finance_onec_cashflow_dashboard", period),
        })
    for source in current["sources"].values():
        if source["missing_months"]:
            signals.append({
                "kind": "missing",
                "title": f"Отсутствуют данные: {source['label']}",
                "detail": ", ".join(
                    comparison_period_label(month, month)
                    for month in source["missing_months"]
                ),
                "detail_type": "text",
                "route_name": source["route_name"],
                "query": _detail_query(source["route_name"], period),
            })
        if source["data_through"] is not None and source["data_through"] < period["last_month"]:
            signals.append({
                "kind": "stale",
                "title": f"Источник отстаёт: {source['label']}",
                "detail": f"Данные по {comparison_period_label(source['data_through'], source['data_through'])}",
                "detail_type": "text",
                "route_name": source["route_name"],
                "query": _detail_query(source["route_name"], period),
            })
    if anomaly_summary["row_count"]:
        query = ""
        if period["first_month"] == period["last_month"]:
            query = urlencode({"period": period["first_month"].strftime("%Y-%m")})
        signals.append({
            "kind": "cost",
            "title": "Продажи без определённой себестоимости",
            "detail": anomaly_summary["row_count"],
            "detail_type": "rows",
            "route_name": "finance_onec_cost_control",
            "query": query,
        })
    return signals


def finance_overview_data(organization, params, *, today=None):
    today = today or timezone.localdate()
    period = resolve_owner_period(params, today=today)
    current = _range_data(
        organization, period["first_month"], period["last_month"]
    )
    comparison_data = []
    for spec in _comparison_specs(period):
        comparison_data.append((
            spec,
            _range_data(
                organization, spec["first"], spec["last"],
                include_freshness=False,
            ),
        ))
    anomalies = get_onec_cost_anomalies(organization).filter(
        period_month__range=(period["first_month"], period["last_month"])
    )
    anomaly_summary = summarize_cost_anomalies(anomalies)
    economy_cards = [
        _card(definition, current, comparison_data, period)
        for definition in ECONOMY_CARDS
    ]
    cashflow_cards = [
        _card(definition, current, comparison_data, period)
        for definition in CASHFLOW_CARDS
    ]
    return {
        "period": period,
        "period_choices": PERIOD_CHOICES,
        "effective_period_label": comparison_period_label(
            period["first_month"], period["last_month"]
        ),
        "economy_cards": economy_cards,
        "cashflow_cards": cashflow_cards,
        "monthly": current["monthly"],
        "seasonality": _seasonality(organization, today),
        "signals": _signals(current, period, anomaly_summary),
        "sources": current["sources"],
        "missing_cost": anomaly_summary,
        # Compatibility keys retained for callers of the existing service API.
        "gross_profit": current["raw"]["profit"],
        "payroll": current["raw"]["payroll"],
        "cashflow": current["raw"]["cashflow"],
        "payroll_accrued": current["values"]["payroll"],
        "freshness": {
            "gross_profit": current["sources"]["profit"],
            "payroll": current["sources"]["payroll"],
            "cashflow": current["sources"]["cashflow"],
        },
        "has_gross_profit_data": current["values"]["revenue"] is not None,
        "has_payroll_data": current["values"]["payroll"] is not None,
        "has_cashflow_data": current["values"]["receipts"] is not None,
    }
