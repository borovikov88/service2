from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.overview import finance_overview_data
from pool_service.finance_imports.profit_dashboard import dashboard_data, resolve_period
from pool_service.finance_imports.payroll_dashboard import payroll_dashboard_data
from pool_service.models import (
    EmployeeOneCIdentity, OneCImportBatch, OneCMonthlyProfit,
    OneCReportPeriodState, Organization, OrganizationAccess, PayrollRow,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class FinanceOverviewTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Overview", paid_until=timezone.now() + timedelta(days=30)
        )
        self.manager = User.objects.create_user("overview-owner", password="pass")
        self.employee = User.objects.create_user("overview-service", password="pass")
        OrganizationAccess.objects.create(user=self.manager, organization=self.organization, role="owner")
        OrganizationAccess.objects.create(user=self.employee, organization=self.organization, role="service")
        self.profit_batch = self._batch(OneCImportBatch.TYPE_MONTHLY_PROFIT, "profit")
        self.payroll_batch = self._batch(OneCImportBatch.TYPE_PAYROLL, "payroll")
        self.identity = EmployeeOneCIdentity.objects.create(
            organization=self.organization, raw_name="Тест", normalized_name="тест",
            normalized_department_name="", source_identity_key="a" * 64,
        )
        for month, revenue, cost in ((date(2026, 1, 1), 100, 60), (date(2026, 2, 1), 200, 100)):
            OneCMonthlyProfit.objects.create(
                organization=self.organization, import_batch=self.profit_batch,
                period_month=month, source_row_number=month.month, nomenclature="Товар",
                nomenclature_type="Товар", revenue=Decimal(revenue), cost=Decimal(cost),
                gross_profit=Decimal(revenue - cost), cost_source="actual",
            )
            PayrollRow.objects.create(
                organization=self.organization, import_batch=self.payroll_batch,
                employee_identity=self.identity, period_month=month, source_row_number=month.month,
                employee_raw_name="Тест", employee_normalized_name="тест", accrued=Decimal("10.00"),
                paid=Decimal("7.00"), opening_balance=Decimal("1.00"), closing_balance=Decimal("4.00"),
            )
            for report_type, batch in ((OneCImportBatch.TYPE_MONTHLY_PROFIT, self.profit_batch), (OneCImportBatch.TYPE_PAYROLL, self.payroll_batch)):
                OneCReportPeriodState.objects.create(
                    organization=self.organization, report_type=report_type, period_month=month,
                    active_batch=batch, updated_by=self.manager,
                )

    def _batch(self, import_type, suffix):
        return OneCImportBatch.objects.create(
            organization=self.organization, import_type=import_type,
            original_filename=f"{suffix}.xlsx", stored_file=f"test/{suffix}.xlsx",
            file_sha256=(suffix * 64)[:64], uploaded_by=self.manager,
            status=OneCImportBatch.STATUS_CONFIRMED,
        )

    def test_composes_existing_trusted_services_with_monthly_periods(self):
        params = {"period": "custom", "start": "2026-01-15", "end": "2026-02-02"}
        data = finance_overview_data(self.organization, params, today=date(2026, 8, 16))
        period = resolve_period(params, today=date(2026, 8, 16))

        self.assertEqual(data["gross_profit"]["totals"], dashboard_data(self.organization, period)["totals"])
        self.assertEqual(data["payroll"]["accrued"], payroll_dashboard_data(self.organization, date(2026, 1, 1), date(2026, 2, 1))["accrued"])
        self.assertEqual(data["payroll_accrued"], Decimal("20.00"))
        self.assertEqual(data["period"]["first_month"], date(2026, 1, 1))
        self.assertEqual(data["period"]["last_month"], date(2026, 2, 1))
        self.assertEqual(data["freshness"]["gross_profit"]["data_through"], date(2026, 2, 1))
        self.assertEqual(data["freshness"]["payroll"]["data_through"], date(2026, 2, 1))

    def test_overview_permissions_and_no_expense_or_cashflow_kpis(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("finance_overview"), {"period": "current_year"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Начисленный ФОТ")
        self.assertNotContains(response, "Чистая прибыль")
        self.assertNotContains(response, "Денежный поток")
        self.assertNotContains(response, "Расходы компании")
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("finance_overview")).status_code, 403)

    def test_no_payroll_data_is_unknown_not_zero(self):
        PayrollRow.objects.all().delete()
        OneCReportPeriodState.objects.filter(report_type=OneCImportBatch.TYPE_PAYROLL).delete()
        data = finance_overview_data(self.organization, {"period": "current_year"}, today=date(2026, 2, 20))
        self.assertFalse(data["has_payroll_data"])
        self.assertIsNone(data["payroll_accrued"])

    def test_period_controls_keep_the_trusted_monthly_boundaries(self):
        today = date(2026, 8, 16)
        cases = {
            "current_month": (date(2026, 8, 1), date(2026, 8, 1)),
            "previous_month": (date(2026, 7, 1), date(2026, 7, 1)),
            "current_year": (date(2026, 1, 1), date(2026, 8, 1)),
            "previous_year": (date(2025, 1, 1), date(2025, 12, 1)),
        }
        for preset, expected in cases.items():
            with self.subTest(preset=preset):
                data = finance_overview_data(self.organization, {"period": preset}, today=today)
                self.assertEqual(
                    (data["period"]["first_month"], data["period"]["last_month"]), expected
                )

        custom = finance_overview_data(
            self.organization,
            {"period": "custom", "start": "2026-01-15", "end": "2026-02-02"},
            today=today,
        )
        self.assertEqual(custom["effective_period_label"], "янв–фев 2026")

    def test_no_gross_profit_data_is_unknown_not_zero(self):
        OneCMonthlyProfit.objects.all().delete()
        OneCReportPeriodState.objects.filter(
            report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT
        ).delete()
        data = finance_overview_data(
            self.organization, {"period": "current_year"}, today=date(2026, 2, 20)
        )
        self.assertFalse(data["has_gross_profit_data"])
        self.assertIsNone(data["freshness"]["gross_profit"]["data_through"])

    def test_overview_get_is_read_only(self):
        self.client.force_login(self.manager)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("finance_overview"))
        self.assertEqual(response.status_code, 200)
        writes = [
            query["sql"] for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            and "django_session" not in query["sql"].lower()
        ]
        self.assertEqual(writes, [])
