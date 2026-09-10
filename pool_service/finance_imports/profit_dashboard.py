from calendar import monthrange
from copy import copy
from datetime import date, datetime
from decimal import Decimal
import re
from uuid import UUID

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


def monthly_profit_summary(organization, first_month, last_month):
    """Return active import-time analytics without building detail breakdowns."""
    rows = list(OneCMonthlyProfit.objects.active_for(organization).filter(
        period_month__range=(first_month, last_month)
    ))
    period_cost_ratio = apply_period_analytics(rows)
    monthly = []
    for month_index in range(
        (last_month.year - first_month.year) * 12
        + last_month.month - first_month.month + 1
    ):
        month = add_months(first_month, month_index)
        monthly.append({
            "month": month,
            **summarize(row for row in rows if row.period_month == month),
        })
    return {
        "rows": rows,
        "totals": summarize(rows),
        "monthly": monthly,
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


def _compatible_display_quantity(revenue_row, cost_row):
    revenue_quantity = revenue_row.quantity
    cost_quantity = cost_row.quantity
    if revenue_quantity in (None, 0):
        return cost_quantity
    if cost_quantity in (None, 0):
        return revenue_quantity
    if revenue_quantity == cost_quantity or revenue_quantity == -cost_quantity:
        return revenue_quantity
    return None


def _presentation_rows(document_rows):
    """Join only an unambiguous revenue/cost movement pair for display."""
    buckets = {}
    order = []
    for row in document_rows:
        source_data = row.source_data if isinstance(row.source_data, dict) else {}
        key = (
            row.period_month,
            source_data.get("source_date"),
            row.nomenclature,
            row.article,
            row.nomenclature_type,
            row.manager_name,
            row.cost_source,
            row.cost_calculation_method,
            row.cost_calculation_ratio,
        )
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(row)

    result = []
    for key in order:
        rows = buckets[key]
        if len(rows) != 2:
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
        if len(revenue_rows) != 1 or len(cost_rows) != 1:
            result.extend(rows)
            continue
        revenue_row = revenue_rows[0]
        cost_row = cost_rows[0]
        quantity = _compatible_display_quantity(revenue_row, cost_row)
        if quantity is None and revenue_row.quantity is not None and cost_row.quantity is not None:
            result.extend(rows)
            continue
        if revenue_row.cost is None or cost_row.cost is None:
            result.extend(rows)
            continue
        merged = copy(revenue_row)
        merged.quantity = quantity
        merged.cost = revenue_row.cost + cost_row.cost
        merged.dashboard_revenue = (
            revenue_row.dashboard_revenue + cost_row.dashboard_revenue
        )
        merged.dashboard_analytical_cost = (
            revenue_row.dashboard_analytical_cost
            + cost_row.dashboard_analytical_cost
        )
        merged.dashboard_gross_profit = (
            (revenue_row.dashboard_gross_profit or Decimal("0"))
            + (cost_row.dashboard_gross_profit or Decimal("0"))
        )
        merged.dashboard_cost_is_calculated = (
            revenue_row.dashboard_cost_is_calculated
            or cost_row.dashboard_cost_is_calculated
        )
        merged.dashboard_unit_price = _display_unit_price(
            merged.dashboard_revenue, merged.quantity
        )
        result.append(merged)
    return result


def customer_breakdown(rows):
    grouped = {}
    for row in rows:
        key = _customer_key(row.customer_name)
        customer = grouped.setdefault(key, {
            "name": re.sub(r"\s+", " ", row.customer_name.strip()) if key else "Покупатель не указан",
            "rows": [], "documents": {},
        })
        customer["rows"].append(row)
        document_key, document_name, can_collapse = _document_group(row)
        document = customer["documents"].setdefault(document_key, {
            "name": document_name,
            "rows": [],
            "can_collapse": can_collapse,
        })
        document["can_collapse"] = document["can_collapse"] and can_collapse
        document["rows"].append(row)
    result = []
    for customer in grouped.values():
        totals = summarize(customer["rows"])
        documents = []
        for document in customer["documents"].values():
            document_rows = document["rows"]
            document_metadata = _document_display_metadata(
                document_rows[0], document["name"], document["can_collapse"]
            )
            documents.append({
                "name": document["name"],
                **document_metadata,
                "managers": sorted({row.manager_name for row in document_rows if row.manager_name}),
                "rows": (
                    _presentation_rows(document_rows)
                    if document["can_collapse"]
                    else document_rows
                ),
                "source_row_count": len(document_rows),
                **summarize(document_rows),
            })
        documents.sort(key=lambda item: (-item["revenue"], item["name"].casefold()))
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
