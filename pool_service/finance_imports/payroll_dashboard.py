from datetime import date
from decimal import Decimal

from django.db.models import Count, Exists, IntegerField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from pool_service.models import EmployeeOneCIdentity, OneCImportBatch, PayrollRow


ZERO = Decimal("0.00")


def month_start(value):
    return value.replace(day=1)


def add_months(value, offset):
    absolute = value.year * 12 + value.month - 1 + offset
    return date(absolute // 12, absolute % 12 + 1, 1)


def default_payroll_period(today=None):
    today = today or date.today()
    return date(today.year, 1, 1), month_start(today)


def parse_payroll_period(period_from, period_to, *, today=None):
    default_from, default_to = default_payroll_period(today)

    def parse(value, default):
        if not value:
            return default
        try:
            parsed = date.fromisoformat(f"{value}-01")
        except (TypeError, ValueError):
            raise ValueError("Укажите период в формате ГГГГ-ММ.")
        return parsed

    first = parse(period_from, default_from)
    last = parse(period_to, default_to)
    if first > last:
        raise ValueError("Начальный месяц не может быть позже конечного.")
    return first, last


def month_sequence(first, last):
    result = []
    current = first
    while current <= last:
        result.append(current)
        current = add_months(current, 1)
    return result


def payroll_dashboard_data(organization, first, last, *, include_personal=False):
    rows = PayrollRow.objects.active_for(
        organization, OneCImportBatch.TYPE_PAYROLL
    ).filter(period_month__range=(first, last))
    totals = rows.aggregate(accrued=Sum("accrued"), paid=Sum("paid"))
    opening = rows.filter(period_month=first).aggregate(value=Sum("opening_balance"))["value"]
    closing = rows.filter(period_month=last).aggregate(value=Sum("closing_balance"))["value"]
    monthly_map = {
        item["period_month"]: item
        for item in rows.values("period_month").annotate(
            accrued=Sum("accrued"), paid=Sum("paid"), closing=Sum("closing_balance")
        ).order_by("period_month")
    }
    months = []
    for period in month_sequence(first, last):
        item = monthly_map.get(period)
        months.append({
            "period_month": period,
            "has_data": item is not None,
            "accrued": item["accrued"] if item else None,
            "paid": item["paid"] if item else None,
            "closing": item["closing"] if item else None,
        })
    data = {
        "accrued": totals["accrued"] or ZERO,
        "paid": totals["paid"] or ZERO,
        "opening": opening,
        "closing": closing,
        "debt_change": (closing - opening) if opening is not None and closing is not None else None,
        "months": months,
        "has_data": rows.exists(),
    }
    if include_personal:
        status_labels = dict(EmployeeOneCIdentity.STATUS_CHOICES)
        people = rows.values(
            "employee_identity_id", "employee_identity__raw_name",
            "employee_identity__department_name", "employee_identity__status",
            "employee_identity__employee__display_name",
        ).annotate(accrued=Sum("accrued"), paid=Sum("paid")).order_by("-accrued")
        boundary = {}
        for item in rows.filter(period_month__in=[first, last]).values(
            "employee_identity_id", "period_month"
        ).annotate(opening=Sum("opening_balance"), closing=Sum("closing_balance")):
            boundary[(item["employee_identity_id"], item["period_month"])] = item
        employees = []
        for item in people:
            identity_id = item["employee_identity_id"]
            first_item = boundary.get((identity_id, first), {})
            last_item = boundary.get((identity_id, last), {})
            item["opening"] = first_item.get("opening")
            item["closing"] = last_item.get("closing")
            item["status_label"] = status_labels.get(
                item["employee_identity__status"], item["employee_identity__status"]
            )
            employees.append(item)
        data["employees"] = employees
    return data


def payroll_identity_rows(organization):
    active_rows = PayrollRow.objects.active_for(
        organization, OneCImportBatch.TYPE_PAYROLL
    ).filter(employee_identity_id=OuterRef("pk"))
    active_count = active_rows.values("employee_identity_id").annotate(
        value=Count("pk")
    ).values("value")[:1]
    return EmployeeOneCIdentity.objects.filter(organization=organization).select_related(
        "employee", "confirmed_by"
    ).annotate(
        active_payroll_row_count=Coalesce(
            Subquery(active_count, output_field=IntegerField()), Value(0)
        ),
        first_active_period=Subquery(
            active_rows.order_by("period_month").values("period_month")[:1]
        ),
        last_active_period=Subquery(
            active_rows.order_by("-period_month").values("period_month")[:1]
        ),
    ).order_by("normalized_name", "id")


def unresolved_active_payroll_identity_count(organization):
    active_rows = PayrollRow.objects.active_for(
        organization, OneCImportBatch.TYPE_PAYROLL
    ).filter(employee_identity_id=OuterRef("pk"))
    return EmployeeOneCIdentity.objects.filter(
        organization=organization,
        status__in=[
            EmployeeOneCIdentity.STATUS_NEEDS_CONFIRMATION,
            EmployeeOneCIdentity.STATUS_NOT_FOUND,
            EmployeeOneCIdentity.STATUS_AMBIGUOUS,
        ],
    ).filter(Exists(active_rows)).count()
