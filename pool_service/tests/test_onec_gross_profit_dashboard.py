import hashlib
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.gross_profit_dashboard import (
    get_gross_profit_dashboard,
)
from pool_service.models import (
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
)


class OneCGrossProfitDashboardTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Дашборд 1С",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.owner = User.objects.create_user("dashboard-owner", password="test")
        OrganizationAccess.objects.create(
            user=self.owner, organization=self.organization, role="owner"
        )
        self.client.force_login(self.owner)

    def batch(self, suffix, organization=None):
        organization = organization or self.organization
        return OneCImportBatch.objects.create(
            organization=organization,
            original_filename=f"{suffix}.xlsx",
            stored_file=f"onec_imports/{suffix}.xlsx",
            file_sha256=hashlib.sha256(suffix.encode()).hexdigest(),
            file_size=1,
            status=OneCImportBatch.STATUS_CONFIRMED,
            uploaded_by=self.owner,
        )

    def row(self, batch, month, row_number, revenue, cost, gross_profit, **extra):
        return OneCMonthlyProfit.objects.create(
            import_batch=batch,
            organization=batch.organization,
            period_month=month,
            source_row_number=row_number,
            nomenclature=f"Строка {row_number}",
            revenue=revenue,
            cost=cost,
            gross_profit=gross_profit,
            **extra,
        )

    def activate(self, batch, month):
        OneCReportPeriodState.objects.create(
            organization=batch.organization,
            report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            period_month=month,
            active_batch=batch,
            updated_by=self.owner,
        )

    def test_uses_only_active_rows_and_analytical_values(self):
        january = date(2026, 1, 1)
        active = self.batch("active")
        historical = self.batch("historical")
        self.row(
            active,
            january,
            1,
            Decimal("1000"),
            None,
            Decimal("1000"),
            calculated_cost=Decimal("600"),
            cost_source=OneCMonthlyProfit.COST_SOURCE_CALCULATED,
            analytical_gross_profit=Decimal("400"),
        )
        self.row(
            historical,
            january,
            1,
            Decimal("9999"),
            Decimal("1"),
            Decimal("9998"),
        )
        self.activate(active, january)

        result = get_gross_profit_dashboard(self.organization)

        self.assertEqual(result["totals"]["revenue"], Decimal("1000"))
        self.assertEqual(result["totals"]["cost"], Decimal("600"))
        self.assertEqual(result["totals"]["gross_profit"], Decimal("400"))
        self.assertEqual(result["totals"]["profitability_percent"], Decimal("40.0000"))
        self.assertEqual(len(result["monthly"]), 1)

    def test_view_has_empty_state_and_requires_management_access(self):
        response = self.client.get(reverse("finance_onec_gross_profit_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Нет активных данных")

        viewer = User.objects.create_user("dashboard-viewer", password="test")
        OrganizationAccess.objects.create(
            user=viewer, organization=self.organization, role="viewer"
        )
        self.client.force_login(viewer)
        self.assertEqual(
            self.client.get(reverse("finance_onec_gross_profit_dashboard")).status_code,
            403,
        )

    def test_dashboard_does_not_include_another_organization(self):
        other = Organization.objects.create(name="Другая организация")
        batch = self.batch("other", organization=other)
        month = date(2026, 2, 1)
        self.row(batch, month, 1, Decimal("500"), Decimal("200"), Decimal("300"))
        self.activate(batch, month)

        result = get_gross_profit_dashboard(self.organization)

        self.assertEqual(result["totals"]["revenue"], Decimal("0.00"))
        self.assertEqual(result["monthly"], [])
