from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.overview import finance_overview_data
from pool_service.finance_imports.cashflow_dashboard import (
    MAX_ARTICLE_CHART_SERIES,
    cashflow_article_trend_data,
    cashflow_dashboard_data,
)
from pool_service.finance_imports.owner_dashboard import resolve_owner_period
from pool_service.finance_imports.profit_dashboard import dashboard_data, resolve_period
from pool_service.finance_imports.payroll_dashboard import payroll_dashboard_data
from pool_service.models import (
    CashFlowRow, EmployeeOneCIdentity, OneCImportBatch, OneCMonthlyProfit,
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
        self.cashflow_batch = self._batch(OneCImportBatch.TYPE_CASHFLOW, "cashflow")
        self.identity = EmployeeOneCIdentity.objects.create(
            organization=self.organization, raw_name="Тест", normalized_name="тест",
            normalized_department_name="", source_identity_key="a" * 64,
        )
        self._add_month(date(2026, 1, 1), revenue=100, cost=60, accrued=10, receipts=80, payments=50)
        self._add_month(date(2026, 2, 1), revenue=200, cost=100, accrued=10, receipts=120, payments=70)

    def _batch(self, import_type, suffix):
        return OneCImportBatch.objects.create(
            organization=self.organization, import_type=import_type,
            original_filename=f"{suffix}.xlsx", stored_file=f"test/{suffix}.xlsx",
            file_sha256=(suffix * 64)[:64], uploaded_by=self.manager,
            status=OneCImportBatch.STATUS_CONFIRMED,
        )

    def _add_month(
        self, month, *, revenue, cost, accrued, receipts, payments,
        profit_batch=None, payroll_batch=None, cashflow_batch=None,
        cost_source=OneCMonthlyProfit.COST_SOURCE_ACTUAL,
    ):
        profit_batch = profit_batch or self.profit_batch
        payroll_batch = payroll_batch or self.payroll_batch
        cashflow_batch = cashflow_batch or self.cashflow_batch
        OneCMonthlyProfit.objects.create(
            organization=self.organization, import_batch=profit_batch,
            period_month=month, source_row_number=month.month, nomenclature="Товар",
            nomenclature_type="Товар", revenue=Decimal(revenue), cost=Decimal(cost),
            gross_profit=Decimal(revenue - cost), cost_source=cost_source,
            analytical_gross_profit=(
                Decimal(revenue - cost)
                if cost_source != OneCMonthlyProfit.COST_SOURCE_UNDEFINED else None
            ),
        )
        PayrollRow.objects.create(
            organization=self.organization, import_batch=payroll_batch,
            employee_identity=self.identity, period_month=month, source_row_number=month.month,
            employee_raw_name="Тест", employee_normalized_name="тест", accrued=Decimal(accrued),
            paid=Decimal("7.00"), opening_balance=Decimal("1.00"), closing_balance=Decimal("4.00"),
        )
        CashFlowRow.objects.create(
            organization=self.organization, import_batch=cashflow_batch,
            period_month=month, source_row_number=month.month,
            article_raw="Продажи", normalized_article_name="продажи", document_raw="Документ",
            receipts=Decimal(receipts), payments=Decimal(payments),
            net_cash_flow=Decimal(receipts) - Decimal(payments),
        )
        for report_type, batch in (
            (OneCImportBatch.TYPE_MONTHLY_PROFIT, profit_batch),
            (OneCImportBatch.TYPE_PAYROLL, payroll_batch),
            (OneCImportBatch.TYPE_CASHFLOW, cashflow_batch),
        ):
            OneCReportPeriodState.objects.update_or_create(
                organization=self.organization, report_type=report_type, period_month=month,
                defaults={"active_batch": batch, "updated_by": self.manager},
            )

    @staticmethod
    def _card(data, key):
        return next(
            card for card in data["economy_cards"] + data["cashflow_cards"]
            if card["key"] == key
        )

    def test_composes_existing_trusted_services_with_monthly_periods(self):
        params = {"period": "custom", "start": "2026-01-15", "end": "2026-02-02"}
        data = finance_overview_data(self.organization, params, today=date(2026, 8, 16))
        period = resolve_period(params, today=date(2026, 8, 16))

        self.assertEqual(data["gross_profit"]["totals"], dashboard_data(self.organization, period)["totals"])
        self.assertEqual(data["payroll"]["accrued"], payroll_dashboard_data(self.organization, date(2026, 1, 1), date(2026, 2, 1))["accrued"])
        self.assertEqual(
            data["cashflow"]["totals"],
            cashflow_dashboard_data(
                self.organization, date(2026, 1, 1), date(2026, 2, 1)
            )["totals"],
        )
        self.assertEqual(data["payroll_accrued"], Decimal("20.00"))
        self.assertEqual(self._card(data, "revenue")["value"], Decimal("300.00"))
        self.assertEqual(self._card(data, "gross_profit")["value"], Decimal("140.00"))
        self.assertEqual(self._card(data, "receipts")["value"], Decimal("200.00"))
        self.assertEqual(self._card(data, "payments")["value"], Decimal("120.00"))
        self.assertEqual(self._card(data, "net_cash_flow")["value"], Decimal("80.00"))
        self.assertEqual(data["period"]["first_month"], date(2026, 1, 1))
        self.assertEqual(data["period"]["last_month"], date(2026, 2, 1))
        self.assertEqual(data["freshness"]["gross_profit"]["data_through"], date(2026, 2, 1))
        self.assertEqual(data["freshness"]["payroll"]["data_through"], date(2026, 2, 1))

    def test_overview_permissions_and_owner_kpis(self):
        self.client.force_login(self.manager)
        response = self.client.get(reverse("finance_overview"), {"period": "current_year"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Начисленный ФОТ")
        self.assertContains(response, "Движение денег")
        self.assertContains(response, "Чистый денежный поток")
        self.assertContains(response, "Группа Аквалайн")
        self.assertNotContains(response, "Чистая прибыль")
        self.assertNotContains(response, "Операционная прибыль")
        self.assertNotContains(response, "Расходы компании")
        self.client.force_login(self.employee)
        self.assertEqual(self.client.get(reverse("finance_overview")).status_code, 403)

    def test_no_payroll_data_is_unknown_not_zero(self):
        PayrollRow.objects.all().delete()
        OneCReportPeriodState.objects.filter(report_type=OneCImportBatch.TYPE_PAYROLL).delete()
        data = finance_overview_data(self.organization, {"period": "current_year"}, today=date(2026, 2, 20))
        self.assertFalse(data["has_payroll_data"])
        self.assertIsNone(data["payroll_accrued"])
        self.assertIsNone(self._card(data, "payroll")["value"])

    def test_period_controls_keep_the_trusted_monthly_boundaries(self):
        today = date(2026, 8, 16)
        cases = {
            "current_month": (date(2026, 8, 1), date(2026, 8, 1)),
            "previous_month": (date(2026, 7, 1), date(2026, 7, 1)),
            "current_year": (date(2026, 1, 1), date(2026, 8, 1)),
            "previous_year": (date(2025, 1, 1), date(2025, 12, 1)),
            "last_12_months": (date(2025, 9, 1), date(2026, 8, 1)),
            "off_season": (date(2025, 11, 1), date(2026, 3, 1)),
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
        self.assertEqual(
            resolve_owner_period(
                {"period": "custom", "start": "2026-03", "end": "2026-04"},
                today=today,
            )["months"],
            [date(2026, 3, 1), date(2026, 4, 1)],
        )

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
        self.assertIsNone(self._card(data, "revenue")["value"])

    def test_no_cashflow_data_is_unknown_not_zero(self):
        CashFlowRow.objects.all().delete()
        OneCReportPeriodState.objects.filter(
            report_type=OneCImportBatch.TYPE_CASHFLOW
        ).delete()
        data = finance_overview_data(
            self.organization, {"period": "current_year"}, today=date(2026, 2, 20)
        )
        self.assertFalse(data["has_cashflow_data"])
        self.assertIsNone(self._card(data, "receipts")["value"])

    def test_incomplete_comparison_keeps_partial_fact_and_hides_all_deltas(self):
        data = finance_overview_data(
            self.organization, {"period": "last_12_months"}, today=date(2026, 8, 20)
        )
        revenue = self._card(data, "revenue")
        self.assertEqual(revenue["value"], Decimal("300.00"))
        self.assertTrue(revenue["partial"])
        for card in data["economy_cards"] + data["cashflow_cards"]:
            self.assertFalse(card["comparisons"][0]["available"])
            self.assertEqual(
                card["comparisons"][0]["label"],
                "Нет полного сопоставимого периода",
            )
            self.assertIsNone(card["comparisons"][0]["absolute"])
            self.assertIsNone(card["comparisons"][0]["percent"])
            self.assertEqual(card["comparisons"][0]["tone"], "neutral")

    def test_preliminary_month_has_neutral_month_and_year_comparisons(self):
        self._add_month(
            date(2025, 8, 1), revenue=90, cost=40, accrued=9,
            receipts=80, payments=50,
        )
        self._add_month(
            date(2026, 7, 1), revenue=100, cost=40, accrued=10,
            receipts=90, payments=60,
        )
        self._add_month(
            date(2026, 8, 1), revenue=120, cost=50, accrued=11,
            receipts=95, payments=70,
        )
        data = finance_overview_data(
            self.organization, {"period": "current_month"}, today=date(2026, 8, 20)
        )
        self.assertTrue(data["period"]["is_preliminary"])
        for key in ("revenue", "payments"):
            card = self._card(data, key)
            self.assertEqual(len(card["comparisons"]), 2)
            self.assertTrue(all(item["available"] for item in card["comparisons"]))
            self.assertTrue(all(item["tone"] == "neutral" for item in card["comparisons"]))

    def test_payment_growth_in_closed_month_is_not_positive(self):
        self._add_month(
            date(2025, 12, 1), revenue=80, cost=30, accrued=8,
            receipts=70, payments=40,
        )
        data = finance_overview_data(
            self.organization,
            {"period": "custom", "start": "2026-01", "end": "2026-01"},
            today=date(2026, 8, 20),
        )
        comparison = self._card(data, "payments")["comparisons"][0]
        self.assertTrue(comparison["available"])
        self.assertEqual(comparison["absolute"], Decimal("10.00"))
        self.assertEqual(comparison["tone"], "negative")

    def test_missing_months_remain_null_and_create_missing_and_stale_signals(self):
        data = finance_overview_data(
            self.organization, {"period": "current_year"}, today=date(2026, 8, 20)
        )
        march = next(item for item in data["monthly"] if item["month"] == date(2026, 3, 1))
        self.assertIsNone(march["revenue"])
        self.assertIsNone(march["payroll"])
        self.assertIsNone(march["net_cash_flow"])
        titles = [signal["title"] for signal in data["signals"]]
        self.assertIn("Отсутствуют данные: Валовая прибыль", titles)
        self.assertIn("Отсутствуют данные: ФОТ", titles)
        self.assertIn("Отсутствуют данные: ДДС", titles)
        self.assertIn("Источник отстаёт: Валовая прибыль", titles)
        self.assertIn("Источник отстаёт: ФОТ", titles)
        self.assertIn("Источник отстаёт: ДДС", titles)

    def test_negative_cash_and_undefined_cost_are_the_only_value_signals(self):
        self._add_month(
            date(2026, 3, 1), revenue=100, cost=0, accrued=15,
            receipts=30, payments=70,
            cost_source=OneCMonthlyProfit.COST_SOURCE_UNDEFINED,
        )
        data = finance_overview_data(
            self.organization,
            {"period": "custom", "start": "2026-03", "end": "2026-03"},
            today=date(2026, 8, 20),
        )
        titles = [signal["title"] for signal in data["signals"]]
        self.assertIn("Отрицательный чистый денежный поток", titles)
        self.assertIn("Продажи без определённой себестоимости", titles)
        self.assertNotIn("Падение валовой прибыли", titles)
        self.assertNotIn("Рост ФОТ", titles)

    def test_seasonality_requires_every_matching_month_for_each_source(self):
        for year in (2025, 2026):
            first_month = 1 if year == 2025 else 3
            for month_number in range(first_month, 9):
                self._add_month(
                    date(year, month_number, 1), revenue=100 + month_number,
                    cost=40, accrued=10, receipts=90, payments=60,
                )
        data = finance_overview_data(
            self.organization, {"period": "current_month"}, today=date(2026, 8, 20)
        )
        for metric in ("gross_profit", "payroll", "net_cash_flow"):
            self.assertTrue(data["seasonality"][metric]["available"])
            self.assertEqual(len(data["seasonality"][metric]["rows"]), 8)

        OneCReportPeriodState.objects.filter(
            organization=self.organization,
            report_type=OneCImportBatch.TYPE_PAYROLL,
            period_month=date(2025, 4, 1),
        ).delete()
        incomplete = finance_overview_data(
            self.organization, {"period": "current_month"}, today=date(2026, 8, 20)
        )
        self.assertFalse(incomplete["seasonality"]["payroll"]["available"])
        self.assertEqual(incomplete["seasonality"]["payroll"]["rows"], [])
        self.assertTrue(incomplete["seasonality"]["gross_profit"]["available"])
        self.assertTrue(incomplete["seasonality"]["net_cash_flow"]["available"])

    def test_overview_and_filtered_cashflow_detail_use_identical_totals(self):
        data = finance_overview_data(
            self.organization,
            {"period": "custom", "start": "2026-01", "end": "2026-02"},
            today=date(2026, 8, 20),
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse("finance_onec_cashflow_dashboard"), {
            "period_from": "2026-01", "period_to": "2026-02",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["totals"], data["cashflow"]["totals"])

    def test_cashflow_detail_formats_every_money_value(self):
        CashFlowRow.objects.create(
            organization=self.organization,
            import_batch=self.cashflow_batch,
            period_month=date(2026, 2, 1),
            source_row_number=99,
            article_raw="Крупное поступление",
            normalized_article_name="крупное поступление",
            document_raw="Документ",
            receipts=Decimal("22786721.74"),
            payments=Decimal("0.00"),
            net_cash_flow=Decimal("22786721.74"),
        )
        self.client.force_login(self.manager)
        response = self.client.get(reverse("finance_onec_cashflow_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "22\u00a0786\u00a0921,74\u00a0₽")
        self.assertContains(response, "120,00\u00a0₽")

    def test_cashflow_article_trend_all_is_one_aggregate_series(self):
        trend = cashflow_article_trend_data(
            self.organization, date(2026, 1, 1), date(2026, 2, 1)
        )
        self.assertEqual(trend["labels"], ["2026-01-01", "2026-02-01"])
        self.assertEqual(len(trend["datasets"]), 1)
        self.assertEqual(trend["datasets"][0]["label"], "Все статьи")
        self.assertEqual(
            trend["datasets"][0]["values"],
            [Decimal("30.00"), Decimal("50.00")],
        )

    def test_cashflow_article_trend_selected_series_fill_active_empty_month(self):
        CashFlowRow.objects.create(
            organization=self.organization,
            import_batch=self.cashflow_batch,
            period_month=date(2026, 1, 1),
            source_row_number=99,
            article_raw="Поставщики",
            normalized_article_name="поставщики",
            document_raw="Документ",
            receipts=Decimal("0.00"),
            payments=Decimal("20.00"),
            net_cash_flow=Decimal("-20.00"),
        )
        trend = cashflow_article_trend_data(
            self.organization,
            date(2026, 1, 1),
            date(2026, 2, 1),
            mode="selected",
            selected_articles=["продажи", "поставщики"],
        )
        self.assertEqual([item["label"] for item in trend["datasets"]], [
            "Продажи", "Поставщики",
        ])
        self.assertEqual(trend["datasets"][0]["values"], [
            Decimal("30.00"), Decimal("50.00"),
        ])
        self.assertEqual(trend["datasets"][1]["values"], [
            Decimal("-20.00"), Decimal("0.00"),
        ])

    def test_cashflow_article_selection_changes_only_chart(self):
        self.client.force_login(self.manager)
        baseline = self.client.get(reverse("finance_onec_cashflow_dashboard"), {
            "period_from": "2026-01", "period_to": "2026-02",
        })
        selected = self.client.get(reverse("finance_onec_cashflow_dashboard"), {
            "period_from": "2026-01", "period_to": "2026-02",
            "article_mode": "selected", "article": ["продажи"],
        })
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.context["totals"], baseline.context["totals"])
        self.assertEqual(selected.context["monthly"], baseline.context["monthly"])
        self.assertEqual(selected.context["articles"], baseline.context["articles"])
        self.assertEqual(
            selected.context["article_trend"]["datasets"][0]["label"],
            "Продажи",
        )
        self.assertContains(selected, "Выбранные статьи")

    def test_cashflow_article_series_limit_is_enforced_server_side(self):
        selected = []
        for index in range(MAX_ARTICLE_CHART_SERIES + 1):
            normalized = f"статья {index}"
            selected.append(normalized)
            CashFlowRow.objects.create(
                organization=self.organization,
                import_batch=self.cashflow_batch,
                period_month=date(2026, 1, 1),
                source_row_number=100 + index,
                article_raw=f"Статья {index}",
                normalized_article_name=normalized,
                document_raw="Документ",
                receipts=Decimal("1.00"),
                payments=Decimal("0.00"),
                net_cash_flow=Decimal("1.00"),
            )
        self.client.force_login(self.manager)
        response = self.client.get(reverse("finance_onec_cashflow_dashboard"), {
            "article_mode": "selected", "article": selected,
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f"Можно одновременно показать не более {MAX_ARTICLE_CHART_SERIES} статей.",
        )
        self.assertEqual(response.context["article_trend"]["datasets"], [])

    def test_cashflow_chart_access_and_get_remain_read_only(self):
        self.client.force_login(self.employee)
        self.assertEqual(
            self.client.get(reverse("finance_onec_cashflow_dashboard")).status_code,
            403,
        )
        self.client.force_login(self.manager)
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(
                reverse("finance_onec_cashflow_dashboard"),
                {"article_mode": "selected", "article": ["продажи"]},
            )
        self.assertEqual(response.status_code, 200)
        writes = [
            query["sql"] for query in queries.captured_queries
            if query["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
            and "django_session" not in query["sql"].lower()
        ]
        self.assertEqual(writes, [])

    def test_all_chartjs_pages_use_month_index_hover(self):
        self.client.force_login(self.manager)
        for route in (
            "finance_overview",
            "finance_onec_profit_dashboard",
            "finance_onec_cashflow_dashboard",
        ):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "interaction:{mode:'index',intersect:false}")
                self.assertContains(response, "hover:{mode:'index',intersect:false}")
                self.assertContains(response, "tooltip:{mode:'index',intersect:false")

    def test_non_active_and_other_organization_rows_never_enter_totals(self):
        historical = self._batch(OneCImportBatch.TYPE_MONTHLY_PROFIT, "historical")
        OneCMonthlyProfit.objects.create(
            organization=self.organization, import_batch=historical,
            period_month=date(2026, 1, 1), source_row_number=1,
            nomenclature="История", nomenclature_type="Товар",
            revenue=Decimal("9999"), cost=Decimal("1"), gross_profit=Decimal("9998"),
            cost_source=OneCMonthlyProfit.COST_SOURCE_ACTUAL,
            analytical_gross_profit=Decimal("9998"),
        )
        other = Organization.objects.create(name="Другая организация")
        other_batch = OneCImportBatch.objects.create(
            organization=other, import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            original_filename="other.xlsx", stored_file="test/other.xlsx",
            file_sha256="f" * 64, uploaded_by=self.manager,
            status=OneCImportBatch.STATUS_CONFIRMED,
        )
        OneCMonthlyProfit.objects.create(
            organization=other, import_batch=other_batch,
            period_month=date(2026, 1, 1), source_row_number=1,
            nomenclature="Чужие данные", nomenclature_type="Товар",
            revenue=Decimal("7777"), cost=Decimal("1"), gross_profit=Decimal("7776"),
            cost_source=OneCMonthlyProfit.COST_SOURCE_ACTUAL,
            analytical_gross_profit=Decimal("7776"),
        )
        OneCReportPeriodState.objects.create(
            organization=other, report_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            period_month=date(2026, 1, 1), active_batch=other_batch,
            updated_by=self.manager,
        )
        data = finance_overview_data(
            self.organization,
            {"period": "custom", "start": "2026-01", "end": "2026-01"},
            today=date(2026, 8, 20),
        )
        self.assertEqual(self._card(data, "revenue")["value"], Decimal("100.00"))

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
