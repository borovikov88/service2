"""Read-only composition of existing trusted Finance analytics."""

from django.db.models import Max

from pool_service.finance_imports.cost_control import (
    get_onec_cost_anomalies,
    summarize_cost_anomalies,
)
from pool_service.finance_imports.payroll_dashboard import payroll_dashboard_data
from pool_service.finance_imports.profit_dashboard import (
    comparison_period_label,
    dashboard_data,
    resolve_period,
)
from pool_service.models import OneCImportBatch, OneCReportPeriodState


def _freshness(organization, report_type):
    states = OneCReportPeriodState.objects.filter(
        organization=organization, report_type=report_type
    )
    return states.aggregate(data_through=Max("period_month"), last_updated=Max("updated_at"))


def finance_overview_data(organization, params, *, today=None):
    """Compose trusted gross-profit, payroll and cost-control view data.

    All source facts are monthly. Day-bounded custom controls include their
    start/end months in full; no daily allocation or proration is performed.
    """
    period = resolve_period(params, today=today)
    gross_profit = dashboard_data(organization, period)
    payroll = payroll_dashboard_data(
        organization, period["first_month"], period["last_month"]
    )
    anomalies = get_onec_cost_anomalies(organization).filter(
        period_month__range=(period["first_month"], period["last_month"])
    )
    anomaly_summary = summarize_cost_anomalies(anomalies)
    gross_freshness = _freshness(organization, OneCImportBatch.TYPE_MONTHLY_PROFIT)
    payroll_freshness = _freshness(organization, OneCImportBatch.TYPE_PAYROLL)

    payroll_accrued = payroll["accrued"] if payroll["has_data"] else None
    return {
        "period": period,
        "effective_period_label": comparison_period_label(
            period["first_month"], period["last_month"]
        ),
        "gross_profit": gross_profit,
        "payroll": payroll,
        "payroll_accrued": payroll_accrued,
        "freshness": {
            "gross_profit": gross_freshness,
            "payroll": payroll_freshness,
        },
        "missing_cost": anomaly_summary,
        "has_gross_profit_data": bool(gross_profit["rows"]),
        "has_payroll_data": payroll["has_data"],
    }
