from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.cashflow_dashboard import (
    cashflow_article_trend_data,
    cashflow_dashboard_data,
)
from pool_service.finance_imports.owner_dashboard import finance_overview_data
from pool_service.finance_imports.management_finance import (
    FLOW_LIQUIDITY,
    get_cashflow_breakdown,
    get_finance_data_status,
    get_monthly_finance,
    get_profit_breakdown,
    management_cashflow_data,
)
from pool_service.models import (
    CashFlowArticleMapping,
    CashFlowRow,
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class ManagementFinanceServiceTests(TestCase):
    month = date(2026, 1, 1)

    def setUp(self):
        self.organization = Organization.objects.create(
            name="Управленческая организация",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.other_organization = Organization.objects.create(name="Чужая организация")
        self.owner = User.objects.create_user("management-owner", password="pass")
        self.viewer = User.objects.create_user("management-viewer", password="pass")
        OrganizationAccess.objects.create(
            organization=self.organization, user=self.owner, role="owner"
        )
        OrganizationAccess.objects.create(
            organization=self.organization, user=self.viewer, role="service"
        )
        self.cashflow_batch = self._batch(
            self.organization, OneCImportBatch.TYPE_CASHFLOW, "cashflow"
        )
        self._activate(self.cashflow_batch, OneCImportBatch.TYPE_CASHFLOW, self.month)

    def _batch(self, organization, report_type, suffix, *, status=None):
        return OneCImportBatch.objects.create(
            organization=organization,
            import_type=report_type,
            original_filename=f"{suffix}.xlsx",
            stored_file=f"test/{suffix}.xlsx",
            file_sha256=(suffix * 64)[:64],
            uploaded_by=self.owner,
            status=status or OneCImportBatch.STATUS_CONFIRMED,
        )

    def _activate(self, batch, report_type, month):
        return OneCReportPeriodState.objects.update_or_create(
            organization=batch.organization,
            report_type=report_type,
            period_month=month,
            defaults={"active_batch": batch, "updated_by": self.owner},
        )[0]

    def _row(self, article, receipts, payments, *, number, batch=None, month=None):
        batch = batch or self.cashflow_batch
        month = month or self.month
        return CashFlowRow.objects.create(
            organization=batch.organization,
            import_batch=batch,
            period_month=month,
            source_row_number=number,
            article_raw=article,
            normalized_article_name=article.casefold(),
            document_raw="Документ",
            receipts=Decimal(receipts),
            payments=Decimal(payments),
            # The canonical read model intentionally recomputes this value.
            net_cash_flow=Decimal("99999.00"),
        )

    def _mapping(
        self,
        article,
        flow_type,
        *,
        status=CashFlowArticleMapping.CLASS_CONFIRMED,
        category="Категория",
        internal=False,
        include_external=True,
    ):
        return CashFlowArticleMapping.objects.create(
            organization=self.organization,
            article_name=article,
            normalized_article_name=article.casefold(),
            management_category=category,
            flow_type=flow_type,
            classification_status=status,
            is_internal_turnover=internal,
            include_in_external_cashflow=include_external,
        )

    @staticmethod
    def _article(data, article):
        return next(item for item in data["articles"] if item["article_raw"] == article)

    @staticmethod
    def _money_sum(*items):
        return {
            key: sum((item[key] for item in items), Decimal("0.00"))
            for key in ("receipts", "payments", "net_cash_flow")
        }

    def _populate_classified_rows(self):
        self._mapping("Операции", CashFlowArticleMapping.FLOW_OPERATING)
        self._mapping("Инвестиции", CashFlowArticleMapping.FLOW_INVESTING)
        self._mapping("Финансы", CashFlowArticleMapping.FLOW_FINANCING)
        self._mapping("Ликвидность", FLOW_LIQUIDITY)
        self._mapping(
            "Внутреннее", CashFlowArticleMapping.FLOW_OPERATING, internal=True
        )
        self._mapping(
            "Исключено", CashFlowArticleMapping.FLOW_FINANCING,
            include_external=False,
        )
        self._mapping(
            "Черновик", CashFlowArticleMapping.FLOW_OPERATING,
            status=CashFlowArticleMapping.CLASS_NEEDS_REVIEW,
            internal=True,
        )
        self._mapping("Без типа", CashFlowArticleMapping.FLOW_UNCLASSIFIED)
        values = (
            ("Операции", "100.00", "20.00"),
            ("Инвестиции", "20.00", "1.00"),
            ("Финансы", "5.00", "2.00"),
            ("Ликвидность", "9.00", "3.00"),
            ("Внутреннее", "7.00", "1.00"),
            ("Исключено", "4.00", "1.00"),
            ("Нет mapping", "2.00", "1.00"),
            ("Черновик", "8.00", "3.00"),
            ("Без типа", "2.00", "0.00"),
        )
        for number, (article, receipts, payments) in enumerate(values, start=1):
            self._row(article, receipts, payments, number=number)

    def test_mapping_semantics_keep_external_equality_and_no_amount_is_lost(self):
        self._populate_classified_rows()
        before_mappings = list(CashFlowArticleMapping.objects.values_list(
            "id", "flow_type", "classification_status", "is_internal_turnover",
            "include_in_external_cashflow",
        ))

        data = management_cashflow_data(self.organization, self.month, self.month)
        external = self._money_sum(
            data["operating"], data["investing"], data["financing"],
            data["liquidity"], data["external_unclassified"],
        )
        self.assertEqual(data["external"], external)
        self.assertEqual(
            data["totals"],
            self._money_sum(data["external"], data["internal"], data["non_external"]),
        )
        self.assertEqual(data["operating"]["net_cash_flow"], Decimal("80.00"))
        self.assertEqual(data["liquidity"]["net_cash_flow"], Decimal("6.00"))
        self.assertNotEqual(data["liquidity"], data["operating"])
        self.assertEqual(data["internal"]["net_cash_flow"], Decimal("6.00"))
        self.assertEqual(data["non_external"]["net_cash_flow"], Decimal("3.00"))

        internal = self._article(data, "Внутреннее")
        self.assertEqual(internal["flow_type"], CashFlowArticleMapping.FLOW_INTERNAL)
        self.assertFalse(internal["is_external"])
        draft = self._article(data, "Черновик")
        self.assertEqual(draft["flow_type"], CashFlowArticleMapping.FLOW_UNCLASSIFIED)
        self.assertTrue(draft["is_external"])
        self.assertEqual(draft["allocation"], "external")
        excluded = self._article(data, "Исключено")
        self.assertEqual(excluded["allocation"], "non_external")
        self.assertNotEqual(excluded["flow_type"], CashFlowArticleMapping.FLOW_INTERNAL)
        registry = {item["article_raw"]: item for item in data["mapping_review_registry"]}
        self.assertIn("mapping_missing", registry["Нет mapping"]["reasons"])
        self.assertIn("classification_not_confirmed", registry["Черновик"]["reasons"])
        self.assertIn(
            "excluded_from_external_requires_decision",
            registry["Исключено"]["reasons"],
        )
        self.assertEqual(
            list(CashFlowArticleMapping.objects.values_list(
                "id", "flow_type", "classification_status", "is_internal_turnover",
                "include_in_external_cashflow",
            )),
            before_mappings,
        )

    def test_only_active_confirmed_rows_for_current_organization_are_aggregated(self):
        self._mapping("Операции", CashFlowArticleMapping.FLOW_OPERATING)
        self._row("Операции", "10.00", "2.00", number=1)
        historical = self._batch(
            self.organization, OneCImportBatch.TYPE_CASHFLOW, "historical"
        )
        self._row("Операции", "900.00", "1.00", number=2, batch=historical)
        preview = self._batch(
            self.organization, OneCImportBatch.TYPE_CASHFLOW, "preview",
            status=OneCImportBatch.STATUS_PREVIEWED,
        )
        preview_month = date(2026, 2, 1)
        self._row("Операции", "700.00", "1.00", number=1, batch=preview, month=preview_month)
        self._activate(preview, OneCImportBatch.TYPE_CASHFLOW, preview_month)
        other_batch = self._batch(
            self.other_organization, OneCImportBatch.TYPE_CASHFLOW, "other"
        )
        self._row("Чужая", "500.00", "1.00", number=1, batch=other_batch)
        self._activate(other_batch, OneCImportBatch.TYPE_CASHFLOW, self.month)

        data = management_cashflow_data(
            self.organization, self.month, preview_month
        )
        self.assertEqual(data["totals"]["receipts"], Decimal("10.00"))
        self.assertEqual(data["totals"]["payments"], Decimal("2.00"))
        self.assertEqual(data["totals"]["net_cash_flow"], Decimal("8.00"))
        self.assertEqual(data["missing_months"], [preview_month])
        self.assertEqual(data["active_months"], [self.month])

    def test_default_cashflow_range_keeps_internal_gaps_and_contract_period(self):
        self._mapping("Операции", CashFlowArticleMapping.FLOW_OPERATING)
        self._row("Операции", "10.00", "2.00", number=1)
        march = date(2026, 3, 1)
        self._activate(self.cashflow_batch, OneCImportBatch.TYPE_CASHFLOW, march)
        self._row("Операции", "30.00", "5.00", number=1, month=march)

        data = management_cashflow_data(self.organization)
        self.assertEqual(
            [item["period_month"] for item in data["monthly"]],
            [self.month, date(2026, 2, 1), march],
        )
        self.assertEqual(data["missing_months"], [date(2026, 2, 1)])
        self.assertFalse(data["monthly"][1]["has_data"])
        self.assertEqual(data["monthly"][1]["breakdown"], [])

        contract = get_cashflow_breakdown(self.organization)
        self.assertEqual(
            contract["period"], {"from": "2026-01-01", "to": "2026-03-01"}
        )
        self.assertEqual(contract["missing_months"], ["2026-02-01"])
        self.assertFalse(contract["complete"])

        status = get_finance_data_status(self.organization)
        self.assertEqual(
            status["period"], {"from": "2026-01-01", "to": "2026-03-01"}
        )
        self.assertEqual(
            status["sources"]["cashflow"]["period"],
            {"from": "2026-01-01", "to": "2026-03-01"},
        )
        self.assertEqual(
            status["sources"]["cashflow"]["missing_months"], ["2026-02-01"]
        )
        self.assertFalse(status["sources"]["cashflow"]["complete"])
        self.assertIn("2026-02-01", status["missing_months"])

    def test_monthly_hierarchy_and_article_chart_share_canonical_buckets(self):
        self._mapping(
            "Повторяющаяся статья",
            CashFlowArticleMapping.FLOW_OPERATING,
            category="Основная деятельность",
        )
        february = date(2026, 2, 1)
        self._row("Повторяющаяся статья", "10.00", "2.00", number=1)
        self._activate(
            self.cashflow_batch, OneCImportBatch.TYPE_CASHFLOW, february
        )
        self._row(
            "Повторяющаяся статья", "30.00", "5.00", number=1,
            month=february,
        )

        data = management_cashflow_data(self.organization, self.month, february)
        january_article = data["monthly"][0]["breakdown"][0]["categories"][0]["articles"][0]
        february_article = data["monthly"][1]["breakdown"][0]["categories"][0]["articles"][0]
        self.assertEqual(january_article["category"], "Основная деятельность")
        self.assertEqual(january_article["net_cash_flow"], Decimal("8.00"))
        self.assertEqual(february_article["net_cash_flow"], Decimal("25.00"))
        self.assertEqual(data["articles"][0]["net_cash_flow"], Decimal("33.00"))
        contract = get_cashflow_breakdown(self.organization, self.month, february)
        self.assertEqual(
            contract["monthly"][1]["breakdown"][0]["categories"][0]["articles"][0]["net_cash_flow"],
            "25.00",
        )

        selected_trend = cashflow_article_trend_data(
            self.organization,
            self.month,
            february,
            mode="selected",
            selected_articles=["повторяющаяся статья"],
            cashflow_data=data,
        )
        self.assertEqual(
            selected_trend["datasets"][0]["values"],
            [Decimal("8.00"), Decimal("25.00")],
        )
        self.assertNotIn(Decimal("99999.00"), selected_trend["datasets"][0]["values"])
        self.assertEqual(
            cashflow_article_trend_data(
                self.organization,
                self.month,
                february,
                mode="selected",
                selected_articles=["повторяющаяся статья"],
            )["datasets"],
            selected_trend["datasets"],
        )

        self.client.force_login(self.owner)
        response = self.client.get(reverse("finance_onec_cashflow_dashboard"), {
            "period_from": "2026-01", "period_to": "2026-02",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["monthly"], data["monthly"])
        content = response.content.decode()
        desktop_start = content.index(
            '<div class="table-responsive cashflow-monthly-desktop">'
        )
        self.assertLess(content.index("cashflow-article-chart"), desktop_start)
        desktop = content[
            desktop_start:content.index(
                '<div class="cashflow-mobile-months">', desktop_start
            )
        ]
        self.assertIn("8,00", desktop)
        self.assertIn("25,00", desktop)
        mobile_start = content.index(
            'data-cashflow-month-breakdown="2026-02"'
        )
        marker = 'data-cashflow-month-article="2026-02:повторяющаяся статья"'
        mobile_detail = content[mobile_start:content.index(marker) + 400]
        self.assertIn("Основная деятельность", mobile_detail)
        self.assertIn("25,00", mobile_detail)

    def test_owner_coverage_ignores_cross_organization_active_batch(self):
        self._mapping("Операции", CashFlowArticleMapping.FLOW_OPERATING)
        self._row("Операции", "10.00", "2.00", number=1)
        foreign_batch = self._batch(
            self.other_organization, OneCImportBatch.TYPE_CASHFLOW, "foreign"
        )
        OneCReportPeriodState.objects.filter(
            organization=self.organization,
            report_type=OneCImportBatch.TYPE_CASHFLOW,
            period_month=self.month,
        ).update(active_batch=foreign_batch)

        overview = finance_overview_data(
            self.organization,
            {"period": "custom", "start": "2026-01", "end": "2026-01"},
            today=date(2026, 8, 20),
        )
        self.assertFalse(overview["sources"]["cashflow"]["has_any"])
        self.assertFalse(overview["has_cashflow_data"])
        self.assertIsNone(overview["monthly"][0]["receipts"])

    def test_contract_uses_decimal_strings_nulls_and_complete_envelope(self):
        self._populate_classified_rows()
        cashflow = get_cashflow_breakdown(
            self.organization, "2026-01", "2026-01"
        )
        self.assertIsInstance(cashflow["as_of"], str)
        self.assertEqual(cashflow["organizations"][0]["id"], self.organization.id)
        self.assertTrue(cashflow["complete"])
        self.assertEqual(cashflow["totals"]["operating"]["receipts"], "100.00")
        self.assertEqual(cashflow["totals"]["external"]["net_cash_flow"], "116.00")
        self.assertIn(str(self.cashflow_batch.id), cashflow["active_batch_ids"])
        self.assertEqual(
            cashflow["active_versions"][0]["report_type"],
            OneCImportBatch.TYPE_CASHFLOW,
        )
        self.assertIn("unclassified_cashflow_articles", {
            item["code"] for item in cashflow["warnings"]
        })
        grouped = get_cashflow_breakdown(
            self.organization,
            "2026-01",
            "2026-01",
            filters={"management_categories": "Категория"},
            group_by="management_category",
        )
        self.assertEqual(grouped["grouped_by"], "management_category")
        self.assertEqual(
            grouped["grouped"][0]["management_category"], "Категория"
        )

        missing = get_monthly_finance(self.organization, "2026-02")
        self.assertFalse(missing["complete"])
        self.assertIsNone(missing["sources"]["cashflow"]["values"]["operating"])
        self.assertIsNone(missing["sources"]["profit"]["values"]["revenue"])
        self.assertIsNone(missing["sources"]["payroll"]["values"]["accrued"])
        status = get_finance_data_status(self.organization)
        self.assertFalse(status["complete"])
        self.assertIn("organizations", status)

    def test_profit_dimensions_are_honestly_unavailable(self):
        profit_batch = self._batch(
            self.organization, OneCImportBatch.TYPE_MONTHLY_PROFIT, "profit"
        )
        OneCMonthlyProfit.objects.create(
            organization=self.organization,
            import_batch=profit_batch,
            period_month=self.month,
            source_row_number=1,
            nomenclature="Услуга",
            nomenclature_type="Услуга",
            manager_name="Менеджер",
            customer_name="Клиент",
            document_name="Реализация",
            revenue=Decimal("100.00"),
            cost=Decimal("40.00"),
            gross_profit=Decimal("60.00"),
            analytical_gross_profit=Decimal("60.00"),
            cost_source=OneCMonthlyProfit.COST_SOURCE_ACTUAL,
        )
        self._activate(profit_batch, OneCImportBatch.TYPE_MONTHLY_PROFIT, self.month)
        result = get_profit_breakdown(self.organization, self.month)
        self.assertTrue(result["available"])
        self.assertEqual(result["totals"]["gross_profit"], "60.00")
        self.assertFalse(result["dimensions"]["department"]["available"])
        self.assertIsNone(result["dimensions"]["department"]["value"])
        self.assertEqual(
            result["dimensions"]["department"]["reason"],
            "active_profit_rows_have_no_department_or_object_dimension",
        )
        for group_by, value in (
            ("manager", "Менеджер"),
            ("client", "Клиент"),
            ("nomenclature", "Услуга"),
            ("document", "Реализация"),
        ):
            with self.subTest(group_by=group_by):
                grouped = get_profit_breakdown(
                    self.organization, self.month, group_by=group_by
                )
                self.assertEqual(
                    grouped["breakdown"][group_by][0][group_by], value
                )
        for group_by in ("department", "object"):
            with self.subTest(group_by=group_by):
                unavailable = get_profit_breakdown(
                    self.organization, self.month, group_by=group_by
                )
                self.assertFalse(unavailable["breakdown"][group_by]["available"])
                self.assertIsNone(unavailable["breakdown"][group_by]["items"])
                self.assertEqual(
                    unavailable["breakdown"][group_by]["reason"],
                    "active_profit_rows_have_no_department_or_object_dimension",
                )

        finance_range = get_monthly_finance(
            self.organization,
            start_month="2026-01",
            end_month="2026-02",
        )
        self.assertIsNone(finance_range["month"])
        self.assertEqual(len(finance_range["monthly"]), 2)
        self.assertEqual(
            finance_range["totals"]["profit"]["gross_margin"], "60.00"
        )
        self.assertEqual(
            finance_range["metadata"]["data_through"]["profit"], "2026-01-01"
        )
        self.assertEqual(
            finance_range["metadata"]["data_through"]["cashflow"], "2026-01-01"
        )
        self.assertIsInstance(
            finance_range["metadata"]["last_updated"]["profit"], str
        )

    def test_status_reports_active_cost_anomaly_and_cashflow_unclassified(self):
        profit_batch = self._batch(
            self.organization, OneCImportBatch.TYPE_MONTHLY_PROFIT, "anomaly"
        )
        OneCMonthlyProfit.objects.create(
            organization=self.organization,
            import_batch=profit_batch,
            period_month=self.month,
            source_row_number=1,
            nomenclature="Товар без себестоимости",
            nomenclature_type="Товар",
            revenue=Decimal("100.00"),
            cost=Decimal("0.00"),
            gross_profit=Decimal("100.00"),
            cost_source=OneCMonthlyProfit.COST_SOURCE_UNDEFINED,
        )
        self._activate(profit_batch, OneCImportBatch.TYPE_MONTHLY_PROFIT, self.month)
        self._row("Без mapping", "3.00", "1.00", number=1)

        status = get_finance_data_status(
            self.organization, self.month, self.month
        )
        self.assertEqual(status["cost_anomalies"]["summary"]["row_count"], 1)
        self.assertEqual(status["cashflow"]["unclassified"]["receipts"], "3.00")
        self.assertIn("cost_anomalies", {item["code"] for item in status["warnings"]})

    def test_dashboard_and_overview_use_the_same_operating_service_values(self):
        self._populate_classified_rows()
        expected = management_cashflow_data(
            self.organization, self.month, self.month
        )
        self.client.force_login(self.owner)
        detail = self.client.get(reverse("finance_onec_cashflow_dashboard"), {
            "period_from": "2026-01", "period_to": "2026-01",
        })
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.context["operating"], expected["operating"])
        self.assertEqual(detail.context["external"], expected["external"])
        self.assertContains(detail, "cashflow-breakdown-mobile")
        self.assertContains(detail, "cashflow-breakdown-desktop")
        self.assertGreaterEqual(
            detail.content.decode().count("Операции"), 2,
        )

        overview = self.client.get(reverse("finance_overview"), {
            "period": "custom", "start": "2026-01", "end": "2026-01",
        })
        self.assertEqual(overview.status_code, 200)
        cards = {item["key"]: item for item in overview.context["cashflow_cards"]}
        self.assertEqual(cards["receipts"]["value"], expected["operating"]["receipts"])
        self.assertEqual(cards["payments"]["value"], expected["operating"]["payments"])
        self.assertEqual(
            cards["net_cash_flow"]["value"], expected["operating"]["net_cash_flow"]
        )
        self.assertContains(overview, "Неклассифицированные статьи ДДС")

    def test_chart_filter_is_isolated_from_management_totals(self):
        self._populate_classified_rows()
        self.client.force_login(self.owner)
        baseline = self.client.get(reverse("finance_onec_cashflow_dashboard"), {
            "period_from": "2026-01", "period_to": "2026-01",
        })
        selected = self.client.get(reverse("finance_onec_cashflow_dashboard"), {
            "period_from": "2026-01", "period_to": "2026-01",
            "article_mode": "selected", "article": ["операции"],
        })
        self.assertEqual(selected.status_code, 200)
        for key in ("totals", "operating", "external", "liquidity", "financing", "internal"):
            self.assertEqual(selected.context[key], baseline.context[key])
        self.assertEqual(selected.context["article_trend"]["datasets"][0]["label"], "Операции")

    def test_read_contract_and_pages_do_not_write_and_keep_guards(self):
        self._populate_classified_rows()
        def source_snapshot():
            return {
                "cashflow_rows": list(
                    CashFlowRow.objects.order_by("pk").values()
                ),
                "import_batches": list(
                    OneCImportBatch.objects.order_by("pk").values()
                ),
                "period_states": list(
                    OneCReportPeriodState.objects.order_by("pk").values()
                ),
            }

        before = source_snapshot()
        with CaptureQueriesContext(connection) as queries:
            management_cashflow_data(self.organization, self.month, self.month)
            get_cashflow_breakdown(self.organization, self.month, self.month)
            get_finance_data_status(self.organization, self.month, self.month)
            get_monthly_finance(self.organization, self.month)
            get_profit_breakdown(self.organization, self.month)
        writes = [
            item["sql"] for item in queries.captured_queries
            if item["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))
        ]
        self.assertEqual(writes, [])
        self.assertEqual(source_snapshot(), before)

        self.client.force_login(self.viewer)
        self.assertEqual(
            self.client.get(reverse("finance_onec_cashflow_dashboard")).status_code,
            403,
        )
        self.assertEqual(self.client.get(reverse("finance_overview")).status_code, 403)

    def test_compatibility_facade_keeps_raw_total_but_net_is_recomputed(self):
        self._mapping("Операции", CashFlowArticleMapping.FLOW_OPERATING)
        self._row("Операции", "12.00", "5.00", number=1)
        data = cashflow_dashboard_data(self.organization, self.month, self.month)
        self.assertEqual(data["totals"]["net_cash_flow"], Decimal("7.00"))
        self.assertEqual(data["operating"]["net_cash_flow"], Decimal("7.00"))
