from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.profit_dashboard import (
    apply_period_analytics,
    comparison_period_label,
    dashboard_data,
    resolve_period,
    summarize,
)
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
                cost="60", calculated=None, stored_ratio="0.5",
                source=OneCMonthlyProfit.COST_SOURCE_ACTUAL,
                customer="", manager="", document=""):
        revenue_value = Decimal(revenue) if revenue is not None else None
        cost_value = Decimal(cost) if cost is not None else None
        calculated_value = Decimal(calculated) if calculated is not None else None
        row = OneCMonthlyProfit.objects.create(
            import_batch=self.batch, organization=self.organization,
            period_month=month, source_row_number=OneCMonthlyProfit.objects.count() + 1,
            customer_name=customer, manager_name=manager, document_name=document,
            article=f"A-{OneCMonthlyProfit.objects.count() + 1}", nomenclature=name,
            nomenclature_type=kind, quantity=Decimal("2"), revenue=revenue_value,
            cost=cost_value,
            gross_profit=(revenue_value - cost_value) if cost_value is not None else None,
            calculated_cost=calculated_value,
            cost_source=source,
            cost_calculation_ratio=Decimal(stored_ratio) if calculated is not None else None,
            analytical_gross_profit=(revenue_value - calculated_value) if calculated is not None else None,
        )
        OneCReportPeriodState.objects.get_or_create(
            organization=self.organization, period_month=month,
            defaults={"active_batch": self.batch, "updated_by": self.user},
        )
        return row

    def test_period_presets_default_and_intersecting_custom_months(self):
        today = date(2026, 8, 15)
        self.assertEqual(resolve_period({}, today)["start"], date(2026, 1, 1))
        self.assertEqual(resolve_period({}, today)["end"], today)
        self.assertEqual(resolve_period({"period": "current_month"}, today)["first_month"], date(2026, 8, 1))
        self.assertEqual(resolve_period({"period": "previous_month"}, today)["first_month"], date(2026, 7, 1))
        self.assertEqual(resolve_period({"period": "current_year"}, today)["start"], date(2026, 1, 1))
        self.assertEqual(resolve_period({"period": "previous_year"}, today)["start"], date(2025, 1, 1))
        self.assertEqual(resolve_period({"period": "last_12_months"}, today)["start"], date(2025, 9, 1))
        month_custom = resolve_period({"period": "custom", "start": "2025-01", "end": "2026-08"}, today)
        self.assertEqual((month_custom["first_month"], month_custom["last_month"]), (date(2025, 1, 1), date(2026, 8, 1)))
        custom = resolve_period({"period": "custom", "start": "2026-02-28", "end": "2026-03-02"}, today)
        self.assertEqual((custom["first_month"], custom["last_month"]), (date(2026, 2, 1), date(2026, 3, 1)))
        for start, end, expected in (
            ("2026-05", "2026-05", 1),
            ("2026-05", "2026-08", 4),
            ("2025-11", "2026-02", 4),
        ):
            with self.subTest(start=start, end=end):
                selected = resolve_period({"period": "custom", "start": start, "end": end}, today)
                months = (
                    (selected["last_month"].year - selected["first_month"].year) * 12
                    + selected["last_month"].month - selected["first_month"].month + 1
                )
                self.assertEqual(months, expected)
        reversed_period = resolve_period(
            {"period": "custom", "start": "2026-08", "end": "2026-05"}, today
        )
        self.assertEqual(reversed_period["first_month"], date(2026, 5, 1))
        self.assertEqual(reversed_period["last_month"], date(2026, 8, 1))
        self.assertTrue(reversed_period["error"])

    def test_comparison_period_rules(self):
        today = date(2026, 8, 15)
        cases = (
            ({"period": "current_month"}, date(2026, 8, 1), date(2026, 8, 1), date(2026, 7, 1), date(2026, 7, 1)),
            ({"period": "previous_month"}, date(2026, 7, 1), date(2026, 7, 1), date(2026, 6, 1), date(2026, 6, 1)),
            ({"period": "current_year"}, date(2026, 1, 1), date(2026, 8, 1), date(2025, 1, 1), date(2025, 8, 1)),
            ({"period": "previous_year"}, date(2025, 1, 1), date(2025, 12, 1), date(2024, 1, 1), date(2024, 12, 1)),
            ({"period": "last_12_months"}, date(2025, 9, 1), date(2026, 8, 1), date(2024, 9, 1), date(2025, 8, 1)),
            ({"period": "custom", "start": "2026-05", "end": "2026-05"}, date(2026, 5, 1), date(2026, 5, 1), date(2025, 5, 1), date(2025, 5, 1)),
            ({"period": "custom", "start": "2026-05", "end": "2026-08"}, date(2026, 5, 1), date(2026, 8, 1), date(2025, 5, 1), date(2025, 8, 1)),
            ({"period": "custom", "start": "2025-11", "end": "2026-02"}, date(2025, 11, 1), date(2026, 2, 1), date(2024, 11, 1), date(2025, 2, 1)),
        )
        for params, first, last, previous_first, previous_last in cases:
            with self.subTest(params=params):
                period = resolve_period(params, today)
                self.assertEqual(period["first_month"], first)
                self.assertEqual(period["last_month"], last)
                self.assertEqual(period["previous_first"], previous_first)
                self.assertEqual(period["previous_last"], previous_last)

    def test_comparison_period_labels(self):
        today = date(2026, 8, 15)
        cases = (
            ({"period": "current_month"}, "июл 2026"),
            ({"period": "current_year"}, "янв–авг 2025"),
            ({"period": "previous_year"}, "2024 год"),
            ({"period": "last_12_months"}, "сен 2024 – авг 2025"),
            ({"period": "custom", "start": "2026-05", "end": "2026-08"}, "май–авг 2025"),
            ({"period": "custom", "start": "2025-11", "end": "2026-02"}, "ноя 2024 – фев 2025"),
        )
        for params, expected in cases:
            with self.subTest(params=params):
                period = resolve_period(params, today)
                self.assertEqual(period["comparison_label"], expected)
                self.assertEqual(
                    comparison_period_label(period["previous_first"], period["previous_last"]),
                    expected,
                )

        self.assertEqual(
            resolve_period({"period": "current_month"}, today)["comparison_reference"],
            "июлю 2026",
        )
        self.assertEqual(
            resolve_period({"period": "previous_year"}, today)["comparison_reference"],
            "2024 году",
        )

    def test_previous_period_kpis_monthly_chart_split_details_and_calculated_cost(self):
        self.add_row(date(2025, 1, 1), revenue="50", cost="30")
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
        self.assertEqual(response.context["totals"]["gross_profit"], Decimal("116.00"))
        self.assertEqual(response.context["comparison"]["revenue"]["absolute"], Decimal("150"))
        self.assertEqual(response.context["comparison"]["revenue"]["percent"], Decimal("300.00"))
        self.assertEqual(len(response.context["monthly"]), 1)
        self.assertEqual(response.context["split"][0]["revenue"], Decimal("140"))
        self.assertEqual(response.context["split"][1]["revenue"], Decimal("60"))
        self.assertContains(response, "Исходная себестоимость 1С")
        self.assertContains(response, "Расчётная · 60,0%")
        self.assertContains(response, "к январю 2025", count=4)
        calculated.refresh_from_db(); service.refresh_from_db()
        self.assertEqual(calculated.cost, Decimal("0"))
        self.assertEqual(service.cost_source, OneCMonthlyProfit.COST_SOURCE_ACTUAL)
        self.assertIsNone(service.calculated_cost)

    def test_selected_period_recalculates_analytical_cost_without_source_writes(self):
        self.add_row(date(2025, 1, 1), name="Предыдущая база", revenue="100", cost="20")
        self.add_row(
            date(2025, 2, 1), name="Предыдущая расчётная", revenue="100", cost="0",
            calculated="80", stored_ratio="0.8",
            source=OneCMonthlyProfit.COST_SOURCE_CALCULATED,
        )
        self.add_row(date(2026, 1, 1), name="Товар A", revenue="100", cost="50")
        self.add_row(date(2026, 2, 1), name="Товар B", revenue="300", cost="240")
        calculated = self.add_row(
            date(2026, 2, 1), name="Товар C", revenue="200", cost="0",
            calculated="10", stored_ratio="0.05",
            source=OneCMonthlyProfit.COST_SOURCE_CALCULATED,
        )
        service = self.add_row(
            date(2026, 2, 1), name="Услуга без себестоимости", kind="Услуга",
            revenue="100", cost=None, calculated="80", stored_ratio="0.8",
            source=OneCMonthlyProfit.COST_SOURCE_CALCULATED,
        )

        response = self.client.get(reverse("finance_onec_profit_dashboard"), {
            "period": "custom", "start": "2026-01-15", "end": "2026-02-02",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["period_cost_ratio"], Decimal("0.7250000000"))
        self.assertEqual(response.context["previous_period_cost_ratio"], Decimal("0.2000000000"))
        self.assertEqual(response.context["totals"]["cost"], Decimal("435.00"))
        self.assertEqual(response.context["comparison"]["cost"]["absolute"], Decimal("395.00"))
        self.assertEqual(response.context["comparison"]["cost"]["percent"], Decimal("987.50"))

        dashboard_rows = {row.pk: row for row in response.context["rows"]}
        self.assertEqual(dashboard_rows[calculated.pk].dashboard_analytical_cost, Decimal("145.00"))
        self.assertEqual(dashboard_rows[calculated.pk].dashboard_gross_profit, Decimal("55.00"))
        self.assertTrue(dashboard_rows[calculated.pk].dashboard_cost_is_calculated)
        self.assertEqual(
            dashboard_rows[calculated.pk].dashboard_period_cost_ratio,
            Decimal("0.7250000000"),
        )
        self.assertIsNone(dashboard_rows[service.pk].dashboard_analytical_cost)
        self.assertFalse(dashboard_rows[service.pk].dashboard_cost_is_calculated)
        self.assertContains(response, "Аналитическая себестоимость")
        self.assertContains(response, "Расчётная · 72,5%")
        self.assertContains(response, "выбранного анализируемого периода")

        calculated.refresh_from_db()
        service.refresh_from_db()
        self.assertEqual(calculated.cost, Decimal("0"))
        self.assertEqual(calculated.calculated_cost, Decimal("10"))
        self.assertEqual(calculated.cost_calculation_ratio, Decimal("0.05"))
        self.assertIsNone(service.cost)
        self.assertEqual(service.calculated_cost, Decimal("80"))
        self.assertEqual(service.cost_calculation_ratio, Decimal("0.8"))

    def test_services_preserve_imported_profit_when_cost_is_null(self):
        january = date(2026, 1, 1)
        primary = self.add_row(
            january, name="Обслуживание бассейна", kind="Услуга",
            revenue="100650", cost=None,
        )
        secondary = self.add_row(
            january, name="Другие услуги", kind="Услуга",
            revenue="29760", cost=None,
        )
        undefined = self.add_row(
            january, name="Услуга без прибыли", kind="Услуга",
            revenue="50", cost=None,
        )
        OneCMonthlyProfit.objects.filter(pk=primary.pk).update(
            gross_profit=Decimal("100650")
        )
        OneCMonthlyProfit.objects.filter(pk=secondary.pk).update(
            gross_profit=Decimal("29760")
        )
        rows = list(OneCMonthlyProfit.objects.filter(pk__in=[primary.pk, secondary.pk, undefined.pk]))

        apply_period_analytics(rows)
        by_id = {row.pk: row for row in rows}
        totals = summarize(rows)

        self.assertEqual(by_id[primary.pk].dashboard_revenue, Decimal("100650"))
        self.assertIsNone(by_id[primary.pk].dashboard_analytical_cost)
        self.assertEqual(by_id[primary.pk].dashboard_gross_profit, Decimal("100650"))
        self.assertEqual(by_id[secondary.pk].dashboard_gross_profit, Decimal("29760"))
        self.assertIsNone(by_id[undefined.pk].dashboard_gross_profit)
        self.assertFalse(any(row.dashboard_cost_is_calculated for row in rows))
        self.assertEqual(totals["gross_profit"], Decimal("130410"))

    def test_january_aggregate_loses_only_calculated_goods_adjustment(self):
        january = date(2026, 1, 1)
        february = date(2026, 2, 1)
        self.add_row(
            january, name="Товары с фактической себестоимостью",
            revenue="236768.44", cost="166655.08",
        )
        calculated_goods = self.add_row(
            january, name="Товар без себестоимости",
            revenue="199.00", cost=None,
        )
        service = self.add_row(
            january, name="Услуги января", kind="Услуга",
            revenue="130410.00", cost=None,
        )
        OneCMonthlyProfit.objects.filter(pk=calculated_goods.pk).update(
            gross_profit=Decimal("199.00")
        )
        OneCMonthlyProfit.objects.filter(pk=service.pk).update(
            gross_profit=Decimal("130410.00")
        )
        self.add_row(
            february, name="База коэффициента февраля",
            revenue="100000.00", cost="37300.93",
        )
        period = resolve_period(
            {"period": "custom", "start": "2026-01-01", "end": "2026-02-28"},
            today=date(2026, 8, 15),
        )

        data = dashboard_data(self.organization, period)
        january_totals = next(
            item for item in data["monthly"] if item["month"] == january
        )

        self.assertEqual(data["period_cost_ratio"], Decimal("0.6056268515"))
        self.assertEqual(january_totals["revenue"], Decimal("367377.44"))
        self.assertEqual(january_totals["cost"], Decimal("166775.60"))
        self.assertEqual(january_totals["gross_profit"], Decimal("200601.84"))
        self.assertEqual(
            Decimal("200722.36") - january_totals["gross_profit"],
            Decimal("120.52"),
        )

    def test_customer_breakdown_combines_months_managers_and_reconciles_totals(self):
        self.add_row(date(2025, 12, 1), name="Товар A", revenue="100", cost="60",
                     customer=" Клиент   А ", manager="Менеджер 1", document="Заказ 1")
        self.add_row(date(2026, 1, 1), name="Услуга", kind="Услуга", revenue="50", cost=None,
                     customer="клиент а", manager="Менеджер 2", document="Заказ 2")
        service = OneCMonthlyProfit.objects.latest("id")
        OneCMonthlyProfit.objects.filter(pk=service.pk).update(gross_profit=Decimal("50"))
        self.add_row(date(2026, 1, 1), name="Скидка 100%", revenue="0", cost="25",
                     customer="Клиент Б", manager="Менеджер 1", document="Заказ 3")
        discounted = OneCMonthlyProfit.objects.latest("id")
        OneCMonthlyProfit.objects.filter(pk=discounted.pk).update(gross_profit=Decimal("-25"))

        data = dashboard_data(self.organization, resolve_period({
            "period": "custom", "start": "2025-12", "end": "2026-01",
        }, today=date(2026, 8, 15)))

        self.assertEqual([item["name"] for item in data["customers"]], ["Клиент А", "Клиент Б"])
        self.assertEqual(data["customers"][0]["revenue"], Decimal("150"))
        self.assertEqual(data["customers"][0]["gross_profit"], Decimal("90"))
        self.assertEqual(data["customers"][1]["gross_profit"], Decimal("-25"))
        self.assertEqual(sum(item["revenue"] for item in data["customers"]), data["totals"]["revenue"])
        self.assertEqual(sum(item["cost"] for item in data["customers"]), data["totals"]["cost"])
        self.assertEqual(sum(item["gross_profit"] for item in data["customers"]), data["totals"]["gross_profit"])

    def test_customer_sorting_and_automatic_month_filter_markup(self):
        self.add_row(date(2026, 1, 1), revenue="10", cost="9", customer="Бета")
        self.add_row(date(2026, 1, 1), revenue="100", cost="20", customer="Альфа")
        response = self.client.get(reverse("finance_onec_profit_dashboard"), {
            "period": "custom", "start": "2026-01", "end": "2026-01",
        })
        self.assertEqual(response.context["page_obj"][0]["name"], "Альфа")
        self.assertContains(response, 'type="month"')
        self.assertContains(response, "periodForm.requestSubmit()")
        self.assertContains(response, "period-loading")
        self.assertContains(response, "document.getElementById('period').value='custom'")

    def test_manager_filter_applies_to_all_dashboard_sections(self):
        self.add_row(
            date(2026, 5, 1), revenue="100", cost="60", customer="Клиент А",
            manager="  Менеджер   А  ", document="Заказ А",
        )
        self.add_row(
            date(2026, 6, 1), revenue="50", cost="20", customer="Клиент А",
            manager="менеджер а", document="Заказ Б",
        )
        self.add_row(
            date(2026, 6, 1), revenue="900", cost="100", customer="Скрытый клиент",
            manager="Менеджер Б", document="Заказ В",
        )
        params = {
            "period": "custom", "start": "2026-05", "end": "2026-06",
            "manager": "Менеджер А",
        }
        response = self.client.get(reverse("finance_onec_profit_dashboard"), params)

        self.assertEqual(response.context["manager"], "Менеджер А")
        self.assertEqual(response.context["totals"]["revenue"], Decimal("150"))
        self.assertEqual(
            [item["revenue"] for item in response.context["monthly"]],
            [Decimal("100"), Decimal("50")],
        )
        self.assertEqual(
            [item["name"] for item in response.context["customers"]], ["Клиент А"]
        )
        self.assertNotContains(response, "Скрытый клиент")

        all_response = self.client.get(reverse("finance_onec_profit_dashboard"), {
            "period": "custom", "start": "2026-05", "end": "2026-06",
        })
        self.assertEqual(all_response.context["totals"]["revenue"], Decimal("1050"))

    def test_manager_filter_and_kpi_comparison_use_prior_year_range(self):
        self.add_row(
            date(2025, 5, 1), revenue="100", cost="60", customer="Клиент А",
            manager="Менеджер А",
        )
        self.add_row(
            date(2025, 5, 1), revenue="1000", cost="100", customer="Чужой клиент",
            manager="Менеджер Б",
        )
        self.add_row(
            date(2026, 5, 1), revenue="150", cost="75", customer="Клиент А",
            manager="Менеджер А",
        )
        period = resolve_period({
            "period": "custom", "start": "2026-05", "end": "2026-05",
        }, today=date(2026, 8, 15))

        data = dashboard_data(self.organization, period, manager="Менеджер А")

        self.assertEqual(data["totals"]["revenue"], Decimal("150"))
        self.assertEqual(data["previous_totals"]["revenue"], Decimal("100"))
        self.assertEqual(data["comparison"]["revenue"], {
            "absolute": Decimal("50"), "percent": Decimal("50.00"),
        })
        self.assertEqual(data["comparison"]["cost"], {
            "absolute": Decimal("15"), "percent": Decimal("25.00"),
        })
        self.assertEqual(data["comparison"]["gross_profit"], {
            "absolute": Decimal("35"), "percent": Decimal("87.50"),
        })
        self.assertEqual(data["comparison"]["profitability"], {
            "absolute": Decimal("10.0000"), "percent": Decimal("25.00"),
        })

    def test_customer_sorting_all_metrics_and_null_profitability_last(self):
        self.add_row(date(2026, 5, 1), revenue="100", cost="50", customer="Альфа", manager="Менеджер А")
        self.add_row(date(2026, 5, 1), revenue="200", cost="180", customer="Бета", manager="Менеджер А")
        self.add_row(date(2026, 5, 1), revenue="0", cost="0", customer="Без выручки", manager="Менеджер А")
        expected = {
            "-revenue": ["Бета", "Альфа", "Без выручки"],
            "revenue": ["Без выручки", "Альфа", "Бета"],
            "-cost": ["Бета", "Альфа", "Без выручки"],
            "cost": ["Без выручки", "Альфа", "Бета"],
            "-gross_profit": ["Альфа", "Бета", "Без выручки"],
            "gross_profit": ["Без выручки", "Бета", "Альфа"],
            "-profitability": ["Альфа", "Бета", "Без выручки"],
            "profitability": ["Бета", "Альфа", "Без выручки"],
        }
        for sort, names in expected.items():
            with self.subTest(sort=sort):
                response = self.client.get(reverse("finance_onec_profit_dashboard"), {
                    "period": "custom", "start": "2026-05", "end": "2026-05",
                    "manager": "Менеджер А", "sort": sort,
                })
                self.assertEqual(
                    [item["name"] for item in response.context["page_obj"]], names
                )
                self.assertContains(response, "manager=%D0%9C%D0%B5%D0%BD%D0%B5%D0%B4%D0%B6%D0%B5%D1%80+%D0%90")
                self.assertContains(response, "start=2026-05")
                self.assertContains(response, "end=2026-05")

    def test_pagination_preserves_filters_and_sort_without_duplicates(self):
        for index in range(51):
            self.add_row(
                date(2026, 5, 1), revenue=str(index + 1), cost="1",
                customer=f"Клиент {index:02d}", manager="Менеджер А",
            )
        response = self.client.get(reverse("finance_onec_profit_dashboard"), {
            "period": "custom", "start": "2026-05", "end": "2026-05",
            "manager": "Менеджер А", "sort": "gross_profit",
        })
        self.assertContains(response, "sort=gross_profit")
        self.assertContains(response, "page=2")
        self.assertNotContains(response, "sort=gross_profit&amp;sort=")

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
            source_row_number=1, nomenclature="Секрет", manager_name="Чужой менеджер",
            revenue=Decimal("999"), cost=Decimal("1"),
        )
        OneCReportPeriodState.objects.create(
            organization=other_org, period_month=date(2026, 1, 1), active_batch=other_batch,
        )
        response = self.client.get(reverse("finance_onec_profit_dashboard"), {"period": "custom", "start": "2026-01-01", "end": "2026-01-31"})
        self.assertNotContains(response, "Секрет")
        self.assertNotContains(response, "Чужой менеджер")
        invalid = self.client.get(reverse("finance_onec_profit_dashboard"), {
            "period": "custom", "start": "2026-01", "end": "2026-01",
            "manager": "Чужой менеджер",
        })
        self.assertEqual(invalid.context["manager"], "")
        manager = User.objects.create_user("manager", password="test")
        OrganizationAccess.objects.create(user=manager, organization=self.organization, role="manager")
        self.client.force_login(manager)
        self.assertEqual(self.client.get(reverse("finance_onec_profit_dashboard")).status_code, 403)
