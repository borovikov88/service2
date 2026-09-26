from calendar import monthrange
from copy import copy
from datetime import date, datetime
from decimal import Decimal
import re
from uuid import UUID

from django.db.models import Sum
from django.utils import timezone

from pool_service.finance_imports.monthly_profit_parser import classify_nomenclature_type
from pool_service.finance_imports.odata_profit import (
    ODataPreviewError,
    normalize_document_type,
)
from pool_service.finance_imports.services import calculate_profitability
from pool_service.models import (
    OneCImportBatch,
    OneCMonthlyProfit,
    onec_monthly_profit_source_identity,
)


PERIOD_CHOICES = (
    ("current_month", "Текущий месяц"),
    ("previous_month", "Прошлый месяц"),
    ("current_year", "Текущий год"),
    ("previous_year", "Прошлый год"),
    ("last_12_months", "Последние 12 месяцев"),
    ("custom", "Произвольный период"),
)
MONTH_SHORT_NAMES = (
    "", "янв", "фев", "мар", "апр", "май", "июн",
    "июл", "авг", "сен", "окт", "ноя", "дек",
)
MONTH_DATIVE_NAMES = (
    "", "январю", "февралю", "марту", "апрелю", "маю", "июню",
    "июлю", "августу", "сентябрю", "октябрю", "ноябрю", "декабрю",
)


def add_months(value, offset):
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def month_end(value):
    return date(value.year, value.month, monthrange(value.year, value.month)[1])


def comparison_period_label(first_month, last_month, *, for_reference=False):
    if (
        first_month.year == last_month.year
        and first_month.month == 1
        and last_month.month == 12
    ):
        suffix = "году" if for_reference else "год"
        return f"{first_month.year} {suffix}"
    if first_month == last_month:
        month_names = MONTH_DATIVE_NAMES if for_reference else MONTH_SHORT_NAMES
        return f"{month_names[first_month.month]} {first_month.year}"
    first_label = MONTH_SHORT_NAMES[first_month.month]
    last_label = MONTH_SHORT_NAMES[last_month.month]
    if first_month.year == last_month.year:
        return f"{first_label}–{last_label} {last_month.year}"
    return f"{first_label} {first_month.year} – {last_label} {last_month.year}"


def resolve_period(params, today=None):
    today = today or timezone.localdate()
    preset = params.get("period", "")
    error = ""
    if preset == "current_month":
        start, end = today.replace(day=1), today
    elif preset == "previous_month":
        start = add_months(today.replace(day=1), -1)
        end = month_end(start)
    elif preset == "current_year":
        start, end = date(today.year, 1, 1), today
    elif preset == "previous_year":
        start, end = date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    elif preset == "last_12_months":
        start, end = add_months(today.replace(day=1), -11), today
    elif preset == "custom":
        try:
            start_value = params.get("start", "")
            end_value = params.get("end", "")
            start = date.fromisoformat(start_value + "-01" if len(start_value) == 7 else start_value)
            parsed_end = date.fromisoformat(end_value + "-01" if len(end_value) == 7 else end_value)
            end = month_end(parsed_end) if len(end_value) == 7 else parsed_end
            if start > end:
                start, end = end.replace(day=1), month_end(start)
                error = "Начало периода было позднее окончания; границы переставлены местами."
        except ValueError:
            start, end = today.replace(day=1), today
            error = "Укажите корректные месяцы начала и окончания периода."
    else:
        start, end, preset = date(today.year, 1, 1), today, ""
    first_month = start.replace(day=1)
    last_month = end.replace(day=1)
    comparison_offset = -1 if preset in {"current_month", "previous_month"} else -12
    previous_first = add_months(first_month, comparison_offset)
    previous_last = add_months(last_month, comparison_offset)
    return {
        "preset": preset, "start": start, "end": end,
        "error": error,
        "first_month": first_month, "last_month": last_month,
        "previous_first": previous_first, "previous_last": previous_last,
        "comparison_label": comparison_period_label(previous_first, previous_last),
        "comparison_reference": comparison_period_label(
            previous_first, previous_last, for_reference=True
        ),
    }


def apply_period_analytics(rows):
    """Expose the persisted per-month import analytics for dashboard summaries."""
    calculated_ratios = set()
    for row in rows:
        revenue = row.revenue or Decimal("0")
        is_goods = classify_nomenclature_type(row.nomenclature_type) == "goods"
        use_stored_calculation = (
            is_goods
            and row.cost_source == OneCMonthlyProfit.COST_SOURCE_CALCULATED
        )
        if use_stored_calculation:
            analytical_cost = row.calculated_cost
            gross_profit = row.analytical_gross_profit
            if row.cost_calculation_ratio is not None:
                calculated_ratios.add(row.cost_calculation_ratio)
        elif row.cost_source == OneCMonthlyProfit.COST_SOURCE_UNDEFINED:
            analytical_cost = None
            gross_profit = None
        else:
            analytical_cost = row.cost
            gross_profit = row.gross_profit

        row.dashboard_revenue = revenue
        row.dashboard_analytical_cost = analytical_cost
        row.dashboard_gross_profit = gross_profit
        row.dashboard_profitability = calculate_profitability(gross_profit, revenue)
        row.dashboard_cost_is_calculated = use_stored_calculation
        row.dashboard_period_cost_ratio = (
            row.cost_calculation_ratio if use_stored_calculation else None
        )
        row.dashboard_unit_price = _display_unit_price(row.revenue, row.quantity)
    return next(iter(calculated_ratios)) if len(calculated_ratios) == 1 else None


def _display_unit_price(revenue, quantity):
    """Return a display-only unit price without changing imported values."""
    if revenue in (None, 0) or quantity in (None, 0):
        return None
    return revenue / quantity


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


def monthly_gross_profit_series(organization, first_month, last_month):
    """Return monthly analytical gross profit without materializing sale rows.

    The query groups by the same fields used by apply_period_analytics so
    nomenclature classification stays in the existing Python helper instead of
    being reimplemented in database-specific SQL.
    """
    grouped = (
        OneCMonthlyProfit.objects.active_for(organization)
        .filter(period_month__range=(first_month, last_month))
        .order_by()
        .values("period_month", "nomenclature_type", "cost_source")
        .annotate(
            source_gross_profit=Sum("gross_profit"),
            analytical_gross_profit=Sum("analytical_gross_profit"),
        )
    )
    months = [
        add_months(first_month, month_index)
        for month_index in range(
            (last_month.year - first_month.year) * 12
            + last_month.month - first_month.month + 1
        )
    ]
    gross_profit_by_month = {month: Decimal("0") for month in months}
    for item in grouped:
        cost_source = item["cost_source"]
        use_stored_calculation = (
            cost_source == OneCMonthlyProfit.COST_SOURCE_CALCULATED
            and classify_nomenclature_type(item["nomenclature_type"]) == "goods"
        )
        if cost_source == OneCMonthlyProfit.COST_SOURCE_UNDEFINED:
            value = Decimal("0")
        elif use_stored_calculation:
            value = item["analytical_gross_profit"] or Decimal("0")
        else:
            value = item["source_gross_profit"] or Decimal("0")
        gross_profit_by_month[item["period_month"]] += value

    return {
        "monthly": [
            {"month": month, "gross_profit": gross_profit_by_month[month]}
            for month in months
        ],
    }

def monthly_profit_summary(organization, first_month, last_month):
    """Return active import-time analytics without building detail breakdowns."""
    rows = list(
        OneCMonthlyProfit.objects.active_for(organization)
        .filter(period_month__range=(first_month, last_month))
        .only(
            "id",
            "period_month",
            "nomenclature_type",
            "quantity",
            "revenue",
            "cost",
            "gross_profit",
            "calculated_cost",
            "cost_source",
            "cost_calculation_ratio",
            "analytical_gross_profit",
        )
        .order_by()
    )
    period_cost_ratio = apply_period_analytics(rows)
    months = [
        add_months(first_month, month_index)
        for month_index in range(
            (last_month.year - first_month.year) * 12
            + last_month.month - first_month.month + 1
        )
    ]
    monthly_totals = {
        month: {
            "revenue": Decimal("0"),
            "cost": Decimal("0"),
            "gross_profit": Decimal("0"),
        }
        for month in months
    }
    totals = {
        "revenue": Decimal("0"),
        "cost": Decimal("0"),
        "gross_profit": Decimal("0"),
    }
    for row in rows:
        revenue = row.dashboard_revenue
        cost = row.dashboard_analytical_cost or Decimal("0")
        gross_profit = row.dashboard_gross_profit or Decimal("0")
        bucket = monthly_totals.get(row.period_month)
        if bucket is not None:
            bucket["revenue"] += revenue
            bucket["cost"] += cost
            bucket["gross_profit"] += gross_profit
        totals["revenue"] += revenue
        totals["cost"] += cost
        totals["gross_profit"] += gross_profit

    def present(values):
        return {
            **values,
            "profitability": calculate_profitability(
                values["gross_profit"], values["revenue"]
            ),
        }

    return {
        "rows": rows,
        "totals": present(totals),
        "monthly": [
            {"month": month, **present(monthly_totals[month])}
            for month in months
        ],
        "period_cost_ratio": period_cost_ratio,
    }


def _customer_key(value):
    normalized = re.sub(r"\s+", " ", (value or "").strip()).lower().replace("ё", "е")
    if normalized in {"", "<покупатель не указан>"}:
        return ""
    return normalized


def _document_group(row):
    source_data = row.source_data if isinstance(row.source_data, dict) else {}
    group_key = source_data.get("document_group_key")
    display = source_data.get("document_display")
    if (
        isinstance(group_key, str)
        and group_key.strip()
        and isinstance(display, str)
        and display.strip()
    ):
        validated_odata_group = _is_validated_odata_group(
            row, source_data, group_key, display
        )
        return ("explicit", group_key), display.strip(), validated_odata_group
    document_name = row.document_name.strip() or "Документ не указан"
    return ("legacy", document_name), document_name, False


_DOCUMENT_LABELS = {
    "Document_РасходнаяНакладная": "Расходная накладная",
    "Document_ОтчетОРозничныхПродажах": "Отчёт о розничных продажах",
    "Document_ЧекККМ": "Чек ККМ",
    "Document_ЗакрытиеМесяца": "Закрытие месяца",
}
_RETAIL_CHECK_TYPE = "Document_ЧекККМ"
_RETAIL_REPORT_TYPE = "Document_ОтчетОРозничныхПродажах"


def _guid(value):
    try:
        normalized = str(UUID(str(value))).lower()
    except (TypeError, ValueError, AttributeError):
        return None
    return normalized if normalized != "00000000-0000-0000-0000-000000000000" else None


def _safe_document_type(value):
    if not isinstance(value, str):
        return None
    try:
        normalized = normalize_document_type(
            f"StandardODATA.{value}", field="Dashboard document type"
        )
    except ODataPreviewError:
        return None
    return normalized if normalized == value else None


def _is_validated_odata_group(row, source_data, group_key, display):
    batch = row.import_batch
    if (
        batch.source_type != OneCImportBatch.SOURCE_ODATA
        or batch.import_type != OneCImportBatch.TYPE_MONTHLY_PROFIT
        or batch.parser_version != "odata-2"
        or batch.status != OneCImportBatch.STATUS_CONFIRMED
        or batch.organization_id != row.organization_id
        or source_data.get("source") != "odata"
        or display != row.document_name
    ):
        return False
    recorder = _guid(source_data.get("recorder"))
    group_recorder = _guid(source_data.get("document_group_recorder"))
    source_recorder = _guid(row.source_recorder)
    recorder_type = source_data.get("recorder_type")
    group_type = source_data.get("document_group_recorder_type")
    if (
        recorder is None
        or group_recorder is None
        or recorder != source_recorder
        or recorder_type not in _DOCUMENT_LABELS
        or group_type not in _DOCUMENT_LABELS
    ):
        return False
    if isinstance(source_data.get("line_number"), bool):
        return False
    try:
        line_number = int(source_data.get("line_number"))
    except (TypeError, ValueError):
        return False
    if line_number != row.source_row_number:
        return False
    expected_identity = onec_monthly_profit_source_identity(
        period_month=row.period_month,
        source_row_number=line_number,
        source_recorder=recorder,
    )
    if row.source_identity != expected_identity:
        return False
    if (group_type, group_recorder) != (recorder_type, recorder) and not (
        recorder_type == _RETAIL_CHECK_TYPE
        and group_type == _RETAIL_REPORT_TYPE
    ):
        return False
    expected_group_key = (
        f"odata-document:{row.organization_id}:{group_type}:{group_recorder}"
    )
    if group_key != expected_group_key:
        return False
    source_org = _guid(source_data.get("organization_guid"))
    document_guid_value = source_data.get("document_guid")
    document_type_value = source_data.get("document_type")
    if document_guid_value is None and document_type_value is None:
        pass
    elif (
        document_guid_value is None
        or document_type_value is None
        or _guid(document_guid_value) is None
        or _safe_document_type(document_type_value) is None
    ):
        return False
    document_number = source_data.get("document_number")
    group_number = source_data.get("document_group_number")
    if (
        source_org is None
        or not isinstance(document_number, str)
        or not document_number.strip()
        or len(document_number) > 100
        or not isinstance(group_number, str)
        or not group_number.strip()
        or len(group_number) > 100
    ):
        return False
    try:
        source_date = date.fromisoformat(source_data.get("source_date"))
        document_date = date.fromisoformat(source_data.get("document_date"))
        group_date = date.fromisoformat(source_data.get("document_group_date"))
        source_period = source_data.get("period")
        if not isinstance(source_period, str) or len(source_period) > 80:
            return False
        period_date = datetime.fromisoformat(
            source_period.replace("Z", "+00:00")
        ).date()
    except (TypeError, ValueError):
        return False
    if period_date != source_date or source_date.replace(day=1) != row.period_month:
        return False
    expected_display = (
        f"{_DOCUMENT_LABELS[group_type]} №{group_number} "
        f"от {group_date:%d.%m.%Y}"
    )
    if display != expected_display:
        return False
    if (group_type, group_recorder) == (recorder_type, recorder):
        return group_number == document_number and group_date == document_date
    return source_date == document_date == group_date


def _document_display_metadata(row, document_name, is_validated):
    """Return safe, readable document details for the presentation layer."""
    fallback = {"label": document_name, "number": "", "date": None}
    if not is_validated:
        return fallback
    source_data = row.source_data if isinstance(row.source_data, dict) else {}
    document_type = source_data.get("document_group_recorder_type")
    number = source_data.get("document_group_number")
    try:
        document_date = date.fromisoformat(source_data.get("document_group_date"))
    except (TypeError, ValueError):
        return fallback
    label = _DOCUMENT_LABELS.get(document_type)
    if not label or not isinstance(number, str) or not number.strip():
        return fallback
    return {"label": label, "number": number.strip(), "date": document_date}


_DIRECT_EXPENSE_ROW_KIND = "direct_order_expense"
_ORDER_TYPE = "Document_ЗаказПокупателя"
_ORDER_LABEL = "Заказ покупателя"


def _row_source_data(row):
    return row.source_data if isinstance(row.source_data, dict) else {}


def _is_direct_expense_row(row):
    return _row_source_data(row).get("row_kind") == _DIRECT_EXPENSE_ROW_KIND


def _resolved_order_group(row):
    """Return validated customer-order metadata for presentation grouping."""
    source_data = _row_source_data(row)
    batch = row.import_batch
    if (
        batch.source_type != OneCImportBatch.SOURCE_ODATA
        or batch.import_type != OneCImportBatch.TYPE_MONTHLY_PROFIT
        or batch.parser_version != "odata-2"
        or batch.status != OneCImportBatch.STATUS_CONFIRMED
        or batch.organization_id != row.organization_id
        or source_data.get("source") != "odata"
    ):
        return None

    order_guid = _guid(source_data.get("resolved_order_guid"))
    order_type = source_data.get("resolved_order_type")
    source_organization_guid = _guid(source_data.get("organization_guid"))
    order_organization_guid = _guid(
        source_data.get("resolved_order_organization_guid")
    )
    order_number = source_data.get("resolved_order_number")
    order_display = source_data.get("resolved_order_display")
    if (
        order_guid is None
        or order_type != _ORDER_TYPE
        or source_organization_guid is None
        or order_organization_guid != source_organization_guid
        or not isinstance(order_number, str)
        or not order_number.strip()
        or len(order_number) > 100
        or not isinstance(order_display, str)
        or not order_display.strip()
    ):
        return None
    try:
        order_date = date.fromisoformat(source_data.get("resolved_order_date"))
    except (TypeError, ValueError):
        return None

    order_number = order_number.strip()
    expected_display = (
        f"{_ORDER_LABEL} №{order_number} от {order_date:%d.%m.%Y}"
    )
    if order_display != expected_display:
        return None
    order_customer_name = None
    resolved_customer_guid = _guid(
        source_data.get("resolved_order_customer_guid")
    )
    resolved_customer_name = source_data.get("resolved_order_customer_name")
    has_customer_metadata = (
        source_data.get("resolved_order_customer_guid") is not None
        or resolved_customer_name is not None
    )
    if has_customer_metadata:
        if (
            resolved_customer_guid is None
            or not isinstance(resolved_customer_name, str)
            or not resolved_customer_name.strip()
        ):
            return None
        order_customer_name = resolved_customer_name.strip()

    if _is_direct_expense_row(row):
        if _guid(source_data.get("direct_expense_order_guid")) != order_guid:
            return None
        source_customer_guid = _guid(source_data.get("customer_guid"))
        if (
            resolved_customer_guid is None
            or resolved_customer_guid != source_customer_guid
            or order_customer_name != row.customer_name
        ):
            return None
    else:
        source_order_guid_value = source_data.get("source_document_order_guid")
        source_order_type = source_data.get("source_document_order_type")
        if source_order_guid_value is not None or source_order_type is not None:
            if (
                _guid(source_order_guid_value) != order_guid
                or source_order_type != _ORDER_TYPE
            ):
                return None

    return {
        "guid": order_guid,
        "display": order_display,
        "label": _ORDER_LABEL,
        "number": order_number,
        "date": order_date,
        "customer_name": order_customer_name,
    }


def _decorate_presentation_row(row):
    source_data = _row_source_data(row)
    row.dashboard_is_direct_expense = _is_direct_expense_row(row)
    row.dashboard_display_nomenclature = row.nomenclature
    row.dashboard_display_type = row.nomenclature_type
    row.dashboard_direct_expense_amount = None
    if row.dashboard_is_direct_expense:
        line_name = source_data.get("direct_expense_line_name")
        content = source_data.get("direct_expense_content")
        if isinstance(line_name, str) and line_name.strip():
            row.dashboard_display_nomenclature = line_name.strip()
        elif (
            row.nomenclature == "Прямые расходы по заказу"
            and isinstance(content, str)
            and content.strip()
        ):
            row.dashboard_display_nomenclature = content.strip()
        row.dashboard_display_type = "Прямые затраты"
        row.dashboard_direct_expense_amount = row.dashboard_analytical_cost
    return row


def _presentation_item_key(row):
    source_data = _row_source_data(row)
    nomenclature_guid = _guid(source_data.get("nomenclature_guid"))
    item_identity = (
        ("guid", nomenclature_guid)
        if nomenclature_guid
        else (
            "text",
            (row.nomenclature or "").strip().casefold(),
            (row.article or "").strip().casefold(),
        )
    )
    return (
        row.period_month,
        source_data.get("source_date"),
        item_identity,
        row.nomenclature_type,
        row.manager_name,
    )



_MONTH_CLOSE_TYPE = "Document_ЗакрытиеМесяца"


def _month_close_adjustment_key(row):
    source_data = _row_source_data(row)
    nomenclature_guid = _guid(source_data.get("nomenclature_guid"))
    item_identity = (
        ("guid", nomenclature_guid)
        if nomenclature_guid
        else (
            "text",
            (row.nomenclature or "").strip().casefold(),
            (row.article or "").strip().casefold(),
        )
    )
    return (
        row.period_month,
        item_identity,
        row.nomenclature_type,
        row.manager_name,
    )


def _merge_order_month_close_adjustments(rows):
    """Fold unambiguous month-close cost corrections into the matching order line."""
    candidates = {}
    for index, row in enumerate(rows):
        source_data = _row_source_data(row)
        if (
            _is_direct_expense_row(row)
            or source_data.get("recorder_type") == _MONTH_CLOSE_TYPE
            or row.dashboard_revenue == 0
        ):
            continue
        candidates.setdefault(_month_close_adjustment_key(row), []).append(index)

    replacements = {}
    consumed = set()
    for index, row in enumerate(rows):
        source_data = _row_source_data(row)
        if (
            source_data.get("recorder_type") != _MONTH_CLOSE_TYPE
            or row.dashboard_revenue != 0
            or row.dashboard_analytical_cost is None
            or row.dashboard_gross_profit is None
        ):
            continue
        targets = candidates.get(_month_close_adjustment_key(row), [])
        if len(targets) != 1:
            continue
        target_index = targets[0]
        target = replacements.get(target_index)
        if target is None:
            target = copy(rows[target_index])
            target.dashboard_month_close_adjustment = Decimal("0")
        target.cost = (target.cost or Decimal("0")) + (row.cost or Decimal("0"))
        target.dashboard_analytical_cost = (
            (target.dashboard_analytical_cost or Decimal("0"))
            + row.dashboard_analytical_cost
        )
        target.dashboard_gross_profit = (
            (target.dashboard_gross_profit or Decimal("0"))
            + row.dashboard_gross_profit
        )
        target.dashboard_month_close_adjustment += row.dashboard_analytical_cost
        replacements[target_index] = target
        consumed.add(index)

    result = []
    for index, row in enumerate(rows):
        if index in consumed:
            continue
        result.append(replacements.get(index, row))
    return result


def _display_quantity_for_movements(revenue_row, cost_rows):
    revenue_quantity = revenue_row.quantity
    cost_quantities = [
        row.quantity for row in cost_rows if row.quantity not in (None, 0)
    ]
    if revenue_quantity not in (None, 0):
        compatible = all(
            quantity in {revenue_quantity, -revenue_quantity}
            for quantity in cost_quantities
        )
        return compatible, revenue_quantity
    if not cost_quantities:
        return True, revenue_quantity
    quantity = cost_quantities[0]
    compatible = all(
        candidate in {quantity, -quantity}
        for candidate in cost_quantities[1:]
    )
    return compatible, quantity


def _presentation_rows(document_rows):
    """Collapse accounting revenue/cost movements into one business line for display."""
    buckets = {}
    order = []
    for row in document_rows:
        if _is_direct_expense_row(row):
            key = ("direct-expense", row.source_identity or row.pk)
        else:
            key = ("business-line",) + _presentation_item_key(row)
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)

    result = []
    for key in order:
        rows = buckets[key]
        if key[0] == "direct-expense" or len(rows) < 2:
            result.extend(rows)
            continue
        revenue_rows = [
            row for row in rows
            if row.dashboard_revenue != 0
            and row.dashboard_analytical_cost == 0
        ]
        cost_rows = [
            row for row in rows
            if row.dashboard_revenue == 0
            and row.dashboard_analytical_cost not in (None, 0)
        ]
        if (
            len(revenue_rows) != 1
            or not cost_rows
            or len(revenue_rows) + len(cost_rows) != len(rows)
            or any(row.cost is None for row in rows)
            or any(row.dashboard_gross_profit is None for row in rows)
        ):
            result.extend(rows)
            continue
        revenue_row = revenue_rows[0]
        quantities_compatible, quantity = _display_quantity_for_movements(
            revenue_row, cost_rows
        )
        if not quantities_compatible:
            result.extend(rows)
            continue

        merged = copy(revenue_row)
        merged.quantity = quantity
        merged.cost = sum((row.cost for row in rows), Decimal("0"))
        merged.dashboard_revenue = sum(
            (row.dashboard_revenue for row in rows), Decimal("0")
        )
        merged.dashboard_analytical_cost = sum(
            (row.dashboard_analytical_cost or Decimal("0") for row in rows),
            Decimal("0"),
        )
        merged.dashboard_gross_profit = sum(
            (row.dashboard_gross_profit for row in rows), Decimal("0")
        )
        merged.dashboard_cost_is_calculated = any(
            row.dashboard_cost_is_calculated for row in rows
        )
        ratios = {
            row.dashboard_period_cost_ratio
            for row in rows
            if row.dashboard_period_cost_ratio is not None
        }
        merged.dashboard_period_cost_ratio = (
            next(iter(ratios)) if len(ratios) == 1 else None
        )
        merged.dashboard_unit_price = _display_unit_price(
            merged.dashboard_revenue, merged.quantity
        )
        result.append(merged)
    return [_decorate_presentation_row(row) for row in result]


def _display_customer_name(value):
    normalized = re.sub(r"\s+", " ", (value or "").strip())
    return normalized or "Покупатель не указан"


def _order_group_customer_name(document):
    validated = document.get("order_customer_names") or []
    if validated:
        validated.sort(
            key=lambda item: (item[0] or date.min, item[1] or 0),
            reverse=True,
        )
        return validated[0][2]

    rows = sorted(
        document["rows"],
        key=lambda row: (row.period_month or date.min, row.pk or 0),
        reverse=True,
    )
    names = [_display_customer_name(row.customer_name) for row in rows]
    for name in names:
        if _customer_key(name) not in {"", "без контрагента"}:
            return name
    return names[0] if names else "Покупатель не указан"


def customer_breakdown(rows):
    # Build validated order groups before customer buckets. This guarantees
    # one order -> one presentation table even when source sale movements have
    # a missing/different register customer while direct costs use the order
    # customer resolved from 1C.
    presentation_groups = {}
    for row in rows:
        document_key, document_name, can_collapse = _document_group(row)
        order = _resolved_order_group(row)
        if order is not None:
            presentation_key = ("order", row.organization_id, order["guid"])
        else:
            presentation_key = (
                "document",
                _customer_key(row.customer_name),
                document_key,
            )

        document = presentation_groups.setdefault(presentation_key, {
            "presentation_key": presentation_key,
            "name": order["display"] if order is not None else document_name,
            "rows": [],
            "subdocuments": {},
            "is_order_group": order is not None,
            "order": order,
            "order_sort_key": (
                (row.period_month or date.min, row.pk or 0)
                if order is not None
                else None
            ),
            "fallback_customer_name": _display_customer_name(row.customer_name),
            "order_customer_names": [],
        })
        document["rows"].append(row)
        if order is not None:
            candidate_order_key = (row.period_month or date.min, row.pk or 0)
            if (
                document["order_sort_key"] is None
                or candidate_order_key > document["order_sort_key"]
            ):
                document["order"] = order
                document["name"] = order["display"]
                document["order_sort_key"] = candidate_order_key
            if order.get("customer_name"):
                document["order_customer_names"].append(
                    (row.period_month, row.pk, order["customer_name"])
                )

        subdocument = document["subdocuments"].setdefault(document_key, {
            "name": document_name,
            "rows": [],
            "can_collapse": can_collapse,
        })
        subdocument["can_collapse"] = (
            subdocument["can_collapse"] and can_collapse
        )
        subdocument["rows"].append(row)

    grouped = {}
    for document in presentation_groups.values():
        customer_name = (
            _order_group_customer_name(document)
            if document["is_order_group"]
            else document["fallback_customer_name"]
        )
        key = _customer_key(customer_name)
        customer = grouped.setdefault(key, {
            "name": customer_name if key else "Покупатель не указан",
            "rows": [],
            "documents": {},
        })
        customer["rows"].extend(document["rows"])
        customer["documents"][document["presentation_key"]] = document

    result = []
    for customer in grouped.values():
        totals = summarize(customer["rows"])
        documents = []
        for document in customer["documents"].values():
            document_rows = document["rows"]
            document_totals = summarize(document_rows)

            presentation_rows = []
            for subdocument in document["subdocuments"].values():
                subdocument_rows = subdocument["rows"]
                if subdocument["can_collapse"]:
                    presentation_rows.extend(_presentation_rows(subdocument_rows))
                else:
                    presentation_rows.extend(
                        _decorate_presentation_row(row)
                        for row in subdocument_rows
                    )
            if document["is_order_group"]:
                presentation_rows = _merge_order_month_close_adjustments(
                    presentation_rows
                )
            presentation_rows.sort(key=lambda row: (
                row.dashboard_is_direct_expense,
                row.period_month,
                (row.dashboard_display_nomenclature or "").casefold(),
                row.pk,
            ))

            order = document["order"]
            if order is not None:
                document_metadata = {
                    "label": order["label"],
                    "number": order["number"],
                    "date": order["date"],
                }
            else:
                first_subdocument = next(iter(document["subdocuments"].values()))
                document_metadata = _document_display_metadata(
                    first_subdocument["rows"][0],
                    document["name"],
                    first_subdocument["can_collapse"],
                )

            direct_expense_rows = [
                row for row in document_rows if _is_direct_expense_row(row)
            ]
            direct_expense_total = sum(
                (
                    row.dashboard_analytical_cost or Decimal("0")
                    for row in direct_expense_rows
                ),
                Decimal("0"),
            )
            is_direct_expense_document = (
                bool(document_rows)
                and len(direct_expense_rows) == len(document_rows)
            )
            source_documents = sorted({
                row.document_name.strip()
                for row in document_rows
                if row.document_name and row.document_name.strip() != document["name"]
            }, key=str.casefold)

            documents.append({
                "name": document["name"],
                **document_metadata,
                "managers": sorted({
                    row.manager_name for row in document_rows if row.manager_name
                }),
                "rows": presentation_rows,
                "source_row_count": len(document_rows),
                "is_order_group": document["is_order_group"],
                "is_direct_expense_document": is_direct_expense_document,
                "has_direct_expenses": bool(direct_expense_rows),
                "direct_expense_total": (
                    direct_expense_total if direct_expense_rows else None
                ),
                "source_documents": source_documents,
                **document_totals,
            })
        documents.sort(key=lambda item: (
            not item["is_order_group"],
            -item["revenue"],
            item["name"].casefold(),
        ))
        result.append({**customer, **totals, "documents": documents})
    result.sort(key=lambda item: (-item["revenue"], item["name"].casefold()))
    return result


def _manager_key(value):
    return re.sub(r"\s+", " ", (value or "").strip()).casefold().replace("ё", "е")


def dashboard_data(organization, period, manager=""):
    all_rows = OneCMonthlyProfit.objects.active_for(organization).select_related(
        "import_batch"
    ).filter(
        period_month__range=(period["previous_first"], period["last_month"])
    )
    current_rows = list(all_rows.filter(
        period_month__range=(period["first_month"], period["last_month"])
    ))
    previous_rows = list(all_rows.filter(
        period_month__range=(period["previous_first"], period["previous_last"])
    ))
    manager_key = _manager_key(manager)
    if manager_key:
        current_rows = [row for row in current_rows if _manager_key(row.manager_name) == manager_key]
        previous_rows = [row for row in previous_rows if _manager_key(row.manager_name) == manager_key]
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
        "customers": customer_breakdown(current_rows),
    }
