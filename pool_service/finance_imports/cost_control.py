from __future__ import annotations

from decimal import Decimal

from django.db.models import Count, Q, Sum

from pool_service.models import OneCImportBatch, OneCMonthlyProfit


TYPE_PATTERNS = {
    "goods": r"^\s*запас\s*$",
    "service": r"^\s*услуга\s*$",
    "work": r"^\s*работа\s*$",
}


def _type_q(kind):
    return Q(nomenclature_type__iregex=TYPE_PATTERNS[kind])


def get_onec_cost_control_dataset(organization, *, period_month=None):
    """Return only the canonical active monthly-profit rows for an organization."""
    rows = OneCMonthlyProfit.objects.active_for(
        organization,
        report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
    )
    if period_month:
        rows = rows.filter(period_month=period_month)
    return rows


def get_onec_cost_anomalies(organization, *, period_month=None, search=""):
    """Find active goods sold with a missing or zero cost."""
    rows = get_onec_cost_control_dataset(
        organization,
        period_month=period_month,
    ).filter(
        _type_q("goods"),
        revenue__gt=0,
    ).filter(Q(cost__isnull=True) | Q(cost=0))
    search = (search or "").strip()
    if search:
        rows = rows.filter(
            Q(nomenclature__icontains=search) | Q(article__icontains=search)
        )
    return rows.select_related("import_batch", "organization").order_by(
        "-revenue", "period_month", "source_row_number", "id"
    )


def summarize_active_dataset(rows):
    summary = rows.aggregate(
        row_count=Count("id"),
        month_count=Count("period_month", distinct=True),
        goods_count=Count("id", filter=_type_q("goods")),
        service_count=Count("id", filter=_type_q("service")),
        work_count=Count("id", filter=_type_q("work")),
    )
    summary["unknown_count"] = summary["row_count"] - (
        summary["goods_count"]
        + summary["service_count"]
        + summary["work_count"]
    )
    return summary


def summarize_cost_anomalies(rows):
    summary = rows.aggregate(
        row_count=Count("id"),
        nomenclature_count=Count("nomenclature", distinct=True),
        revenue=Sum("revenue"),
        cost=Sum("cost"),
        gross_profit=Sum("gross_profit"),
    )
    for key in ("revenue", "cost", "gross_profit"):
        summary[key] = summary[key] or Decimal("0.00")
    return summary


def monthly_cost_anomaly_summary(rows):
    return rows.order_by().values("period_month").annotate(
        row_count=Count("id"),
        revenue=Sum("revenue"),
    ).order_by("period_month")


def available_cost_control_months(organization):
    return (
        get_onec_cost_control_dataset(organization)
        .order_by("period_month")
        .values_list("period_month", flat=True)
        .distinct()
    )
