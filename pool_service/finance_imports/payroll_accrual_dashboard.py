"""Read-only accrued FOT: one active source per month, never payments or debt."""

from decimal import Decimal

from django.db.models import F, Sum

from pool_service.models import (
    OneCImportBatch, OneCReportPeriodState, PayrollAccrualMonth, PayrollRow,
)
from .payroll_dashboard import month_sequence


def accrual_dashboard_data(organization, first, last, *, include_freshness=True):
    """Prefer confirmed OData accruals; retain Excel as the monthly fallback.

    Read rows against validated active batches, rather than interpreting a state
    without rows as zero. A preview, broken cross-org link or missing month cannot
    provide financial coverage. This service does not access 1C or write data.
    """
    types = (OneCImportBatch.TYPE_PAYROLL, OneCImportBatch.TYPE_PAYROLL_ACCRUAL)
    state_query = OneCReportPeriodState.objects.filter(
        organization=organization, report_type__in=types,
        active_batch__organization=organization,
        active_batch__status=OneCImportBatch.STATUS_CONFIRMED,
        active_batch__import_type=F("report_type"),
    ).select_related("active_batch")
    if not include_freshness:
        state_query = state_query.filter(period_month__range=(first, last))
    states_by_key = {
        (state.active_batch_id, state.period_month): state for state in state_query
    }
    batches = {key[0] for key in states_by_key}
    selected = {}
    # The Excel ledger remains unchanged. Aggregate only the accrued column.
    for row in PayrollRow.objects.filter(
        organization=organization, import_batch_id__in=batches,
        import_batch__organization=organization,
        import_batch__import_type=OneCImportBatch.TYPE_PAYROLL,
    ).values("import_batch_id", "period_month").annotate(accrued=Sum("accrued")):
        state = states_by_key.get((row["import_batch_id"], row["period_month"]))
        if state:
            selected[row["period_month"]] = {
                "accrued": row["accrued"], "state": state,
                "source": "excel", "source_label": "Excel", "currency_guid": None,
            }
    for row in PayrollAccrualMonth.objects.filter(
        organization=organization, import_batch_id__in=batches,
        import_batch__organization=organization,
        import_batch__import_type=OneCImportBatch.TYPE_PAYROLL_ACCRUAL,
        import_batch__source_type=OneCImportBatch.SOURCE_ODATA,
    ):
        state = states_by_key.get((row.import_batch_id, row.period_month))
        if state:
            selected[row.period_month] = {
                "accrued": row.accrued, "state": state,
                "source": "odata", "source_label": "1С", "currency_guid": row.currency_guid,
            }
    months = []
    for month in month_sequence(first, last):
        item = selected.get(month)
        months.append({
            "period_month": month, "has_data": item is not None,
            "accrued": item["accrued"] if item else None,
            "source": item["source"] if item else None,
            "source_label": item["source_label"] if item else "Нет данных",
        })
    period_items = {month: item for month, item in selected.items() if first <= month <= last}
    currencies = {item["currency_guid"] for item in period_items.values() if item["currency_guid"]}
    currency_conflict = len(currencies) > 1
    available = [item["accrued"] for item in period_items.values()]
    return {
        "months": months,
        "accrued": sum(available, Decimal("0.00")) if available and not currency_conflict else None,
        "has_data": bool(available) and not currency_conflict,
        "currency_conflict": currency_conflict,
        "states": {month: item["state"] for month, item in period_items.items()} if not currency_conflict else {},
        "data_through": max(selected, default=None) if include_freshness else None,
        "last_updated": max((item["state"].updated_at for item in selected.values()), default=None) if include_freshness else None,
    }
