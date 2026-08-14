from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.profit_dashboard import resolve_period
from pool_service.models import (
    OneCImportBatch, OneCMonthlyProfit, OneCReportPeriodState, Organization,
    OrganizationAccess,
)


class ProfitDashboardTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Основная", paid_until=timezone.now() + timedelta(days=30)
        )
        self.user = User.objects.create_user("accountant", password="test")
        OrganizationAccess.objects.create(
            user=self.user, organization=self.organization, role="accountant"
        )
        self.client.force_login(self.user)
        self.batch = OneCImportBatch.objects.create(
            organization=self.organization, original_filename="report.xlsx",
            file_sha256="a" * 64, uploaded_by=self.user,
            status=OneCImportBatch.STATUS_CONFIRMED,
        )

    def add_row(self, month, *, name="Товар", kind="Товар", revenue="100",
                cost="60", calculated=None, source=OneCMonthlyProfit.COST_SOURCE_ACTUAL):
        row = OneCMonthlyProfit.objects.create(
            import_batch=self.batch, organization=self.organization,
            period_month=month, source_row_number=OneCMonthlyProfit.objects.count() + 1,
            article=f"A-{OneCMonthlyProfit.objects.count() + 1}", nomenclature=name,
            nomenclature_type=kind, quantity=Decimal("2"), revenue=Decimal(revenue),
            cost=Decimal(cost), gross_profit=Decimal(revenue) - Decimal(cost),
            calculated_cost=Decimal(calculated) if calculated is not None else None,
            cost_source=source,
            cost_calculation_ratio=Decimal("0.5") if calculated is not None else None,
            analytical_gross_profit=(Decimal(revenue) - Decimal(calculated)) if calculated is not None else None,
        )
        OneCReportPeriodState.objects.get_or_create(
            organization=self.organization, period_month=month,
            defaults={"active_batch": self.batch, "updated_by": self.user},
        )
        return row

    def test_period_presets_default_and_intersecting_custom_months(self):
        today = date(2026, 8, 14)
        self.assertEqual(resolve_period({}, today)["start"], date(2026, 1, 1))
        self.assertEqual(resolve_period({}, today)["end"], today)
        self.assertEqual(resolve_period({"period": "current_month"}, today)["first_month"], date(2026, 8, 1))
        self.assertEqual(resolve_period({"period": "previous_month"}, today)["first_month"], date(2026, 7, 1))
        self.assertEqual(resolve_period({"period": "current_year"}, today)["start"], date(2026, 1, 1))
        self.assertEqual(resolve_period({"period": "previous_year"}, today)["start"], date(2025, 1, 1))
        custom = resolve_period({"period": "custom", "start": "2026-02-28", "end": "2026-03-02"}, today)
        self.assertEqual((custom["first_month"], custom["last_month"]), (date(2026, 2, 1), date(2026, 3, 1)))

    def test_previous_period_kpis_monthly_chart_split_details_and_calculated_cost(self):
        self.add_row(date(2025, 12, 1), revenue="50", cost="30")
        self.add_row(date(2026, 1, 1), revenue="100", cost="60")
        calculated = self.add_row(
            date(2026, 1, 1), name="Расчётный товар", revenue="40", cost="0",
            calculated="20", source=OneCMonthlyProfit.COST_SOURCE_CALCULATED,
        )
        service = self.add_row(date(2026, 1, 1), name="Услуга", kind="Услуга", revenue="60", cost="0")
        response = self.client.get(reverse("finance_onec_profit_dashboard"), {
            "period": "custom", "start": "2026-01-15", "end": "2026-01-20",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["totals"]["revenue"], Decimal("200"))
        self.assertEqual(response.context["totals"]["gross_profit"], Decimal("120"))
        self.assertEqual(response.context["comparison"]["revenue"]["absolute"], Decimal("150"))
        self.assertEqual(response.context["comparison"]["revenue"]["percent"], Decimal("300.00"))
        self.assertEqual(len(response.context["monthly"]), 1)
        self.assertEqual(response.context["split"][0]["revenue"], Decimal("140"))
        self.assertEqual(response.context["split"][1]["revenue"], Decimal("60"))
        self.assertContains(response, "Исходная себестоимость 1С")
        self.assertContains(response, "Расчётная · 50,0%")
        calculated.refresh_from_db(); service.refresh_from_db()
        self.assertEqual(calculated.cost, Decimal("0"))
        self.assertEqual(service.cost_source, OneCMonthlyProfit.COST_SOURCE_ACTUAL)
        self.assertIsNone(service.calculated_cost)

    def test_sorting(self):
        low = self.add_row(date(2026, 1, 1), name="Низкая", revenue="10", cost="9")
        high = self.add_row(date(2026, 1, 1), name="Высокая", revenue="100", cost="20")
        for sort, expected in (("-revenue", high), ("revenue", low), ("-gross_profit", high), ("-profitability", high)):
            with self.subTest(sort=sort):
                response = self.client.get(reverse("finance_onec_profit_dashboard"), {
                    "period": "custom", "start": "2026-01-01", "end": "2026-01-31", "sort": sort,
                })
                self.assertEqual(response.context["page_obj"][0].pk, expected.pk)

    def test_permissions_and_organization_isolation(self):
        self.add_row(date(2026, 1, 1))
        other_org = Organization.objects.create(name="Другая", paid_until=timezone.now() + timedelta(days=30))
        other_user = User.objects.create_user("other", password="test")
        OrganizationAccess.objects.create(user=other_user, organization=other_org, role="accountant")
        other_batch = OneCImportBatch.objects.create(
            organization=other_org, original_filename="other.xlsx", file_sha256="b" * 64,
            uploaded_by=other_user, status=OneCImportBatch.STATUS_CONFIRMED,
        )
        OneCMonthlyProfit.objects.create(
            import_batch=other_batch, organization=other_org, period_month=date(2026, 1, 1),
            source_row_number=1, nomenclature="Секрет", revenue=Decimal("999"), cost=Decimal("1"),
        )
        OneCReportPeriodState.objects.create(
            organization=other_org, period_month=date(2026, 1, 1), active_batch=other_batch,
        )
        response = self.client.get(reverse("finance_onec_profit_dashboard"), {"period": "custom", "start": "2026-01-01", "end": "2026-01-31"})
        self.assertNotContains(response, "Секрет")
        manager = User.objects.create_user("manager", password="test")
        OrganizationAccess.objects.create(user=manager, organization=self.organization, role="manager")
        self.client.force_login(manager)
        self.assertEqual(self.client.get(reverse("finance_onec_profit_dashboard")).status_code, 403)
