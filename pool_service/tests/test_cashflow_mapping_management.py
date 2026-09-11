from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.employee_matching import normalize_onec_name
from pool_service.finance_imports.management_finance import (
    CASHFLOW_SOURCE_PREVIEW_LIMIT,
    _safe_cashflow_source_display,
    cashflow_article_source_previews,
    get_cashflow_breakdown,
    management_cashflow_data,
)
from pool_service.models import (
    CashFlowArticleMapping,
    CashFlowRow,
    DataAuditLog,
    OneCImportBatch,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
)
from pool_service.services.cashflow_classification import (
    save_explicit_cashflow_mapping,
)


@override_settings(ALLOWED_HOSTS=["testserver"])
class CashFlowMappingManagementTests(TestCase):
    month = date(2026, 1, 1)

    def setUp(self):
        self.organization = Organization.objects.create(
            name="Основная организация",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.other_organization = Organization.objects.create(name="Другая организация")
        self.owner = User.objects.create_user("cashflow-owner", password="pass")
        self.viewer = User.objects.create_user("cashflow-viewer", password="pass")
        OrganizationAccess.objects.create(
            organization=self.organization, user=self.owner, role="owner"
        )
        OrganizationAccess.objects.create(
            organization=self.organization, user=self.viewer, role="service"
        )
        self.batch = self._batch(self.organization, "active")
        self._activate(self.organization, self.batch, self.month)
        self.client.force_login(self.owner)

    def _batch(self, organization, suffix, *, status=OneCImportBatch.STATUS_CONFIRMED):
        return OneCImportBatch.objects.create(
            organization=organization,
            import_type=OneCImportBatch.TYPE_CASHFLOW,
            original_filename=f"{suffix}.xlsx",
            stored_file=f"test/{suffix}.xlsx",
            file_sha256=(suffix * 64)[:64],
            uploaded_by=self.owner,
            status=status,
        )

    def _activate(self, organization, batch, month):
        return OneCReportPeriodState.objects.update_or_create(
            organization=organization,
            report_type=OneCImportBatch.TYPE_CASHFLOW,
            period_month=month,
            defaults={"active_batch": batch, "updated_by": self.owner},
        )[0]

    def _row(
        self, article, receipts, payments, *, number, batch=None,
        month=None, organization=None, document_raw="Документ",
        source_reference="", source_data=None,
    ):
        batch = batch or self.batch
        organization = organization or batch.organization
        month = month or self.month
        return CashFlowRow.objects.create(
            organization=organization,
            import_batch=batch,
            period_month=month,
            source_row_number=number,
            article_raw=article,
            normalized_article_name=normalize_onec_name(article),
            source_reference=source_reference,
            document_raw=document_raw,
            receipts=Decimal(receipts),
            payments=Decimal(payments),
            net_cash_flow=Decimal("99999.00"),
            source_data=source_data or {},
        )

    def _post_data(self, article, *, flow_type, status, category="Категория", **extra):
        return {
            "article": article,
            "period_from": "2026-01",
            "period_to": "2026-01",
            "flow_type": flow_type,
            "classification_status": status,
            "management_category": category,
            "comment": extra.pop("comment", "Ручное решение"),
            **extra,
        }

    def _source_snapshot(self):
        return {
            "rows": list(CashFlowRow.objects.order_by("pk").values()),
            "batches": list(OneCImportBatch.objects.order_by("pk").values()),
            "states": list(OneCReportPeriodState.objects.order_by("pk").values()),
        }

    def test_management_screen_reads_only_active_confirmed_articles(self):
        self._row("Активная статья", "100.00", "20.00", number=1)
        historical = self._batch(self.organization, "historical")
        self._row("Неактивная статья", "900.00", "1.00", number=1, batch=historical)
        preview = self._batch(
            self.organization, "preview", status=OneCImportBatch.STATUS_PREVIEWED
        )
        self._row("Черновая статья", "700.00", "1.00", number=1, batch=preview)
        foreign = self._batch(self.other_organization, "foreign")
        self._activate(self.other_organization, foreign, self.month)
        self._row(
            "Чужая статья", "500.00", "1.00", number=1,
            batch=foreign, organization=self.other_organization,
        )
        before = self._source_snapshot()

        response = self.client.get(reverse("finance_onec_cashflow_mapping"), {
            "period_from": "2026-01", "period_to": "2026-01",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Активная статья")
        self.assertNotContains(response, "Неактивная статья")
        self.assertNotContains(response, "Черновая статья")
        self.assertNotContains(response, "Чужая статья")
        self.assertEqual(CashFlowArticleMapping.objects.count(), 0)
        self.assertFalse(DataAuditLog.objects.filter(
            entity_type="CashFlowArticleMapping"
        ).exists())
        self.assertEqual(self._source_snapshot(), before)

    def test_access_is_server_guarded_and_save_route_is_post_only(self):
        self._row("Статья", "10.00", "1.00", number=1)
        mapping_url = reverse("finance_onec_cashflow_mapping")
        save_url = reverse("finance_onec_cashflow_mapping_save")

        self.assertEqual(self.client.get(mapping_url).status_code, 200)
        self.assertContains(
            self.client.get(reverse("finance_onec_cashflow_dashboard")), mapping_url
        )
        self.assertEqual(self.client.get(save_url).status_code, 405)

        self.client.force_login(self.viewer)
        self.assertEqual(self.client.get(mapping_url).status_code, 403)
        self.assertEqual(self.client.post(save_url, self._post_data(
            "статья",
            flow_type=CashFlowArticleMapping.FLOW_OPERATING,
            status=CashFlowArticleMapping.CLASS_CONFIRMED,
            confirmation="on",
        )).status_code, 403)
        self.assertFalse(CashFlowArticleMapping.objects.exists())

    def test_post_requires_csrf_and_explicit_confirmation_for_confirmed_mapping(self):
        article = "  СЧЁТ   Ёлка "
        self._row(article, "10.00", "1.00", number=1)
        save_url = reverse("finance_onec_cashflow_mapping_save")
        data = self._post_data(
            "счет елка",
            flow_type=CashFlowArticleMapping.FLOW_OPERATING,
            status=CashFlowArticleMapping.CLASS_CONFIRMED,
            confirmation="on",
        )
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        self.assertEqual(csrf_client.post(save_url, data).status_code, 403)
        self.assertFalse(CashFlowArticleMapping.objects.exists())

        missing_confirmation = dict(data)
        missing_confirmation.pop("confirmation")
        response = self.client.post(save_url, missing_confirmation)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CashFlowArticleMapping.objects.exists())

        before = self._source_snapshot()
        response = self.client.post(save_url, data)
        self.assertRedirects(
            response,
            reverse("finance_onec_cashflow_mapping")
            + "?period_from=2026-01&period_to=2026-01",
            fetch_redirect_response=False,
        )
        mapping = CashFlowArticleMapping.objects.get()
        self.assertEqual(mapping.article_name, article)
        self.assertEqual(mapping.normalized_article_name, "счет елка")
        self.assertEqual(mapping.flow_type, CashFlowArticleMapping.FLOW_OPERATING)
        self.assertEqual(mapping.classification_status, CashFlowArticleMapping.CLASS_CONFIRMED)
        self.assertFalse(mapping.is_internal_turnover)
        self.assertTrue(mapping.include_in_external_cashflow)
        self.assertEqual(mapping.updated_by, self.owner)
        audit = DataAuditLog.objects.get(
            entity_type="CashFlowArticleMapping", entity_id=str(mapping.pk)
        )
        self.assertEqual(audit.action, DataAuditLog.ACTION_CREATE)
        self.assertEqual(audit.actor, self.owner)
        self.assertEqual(self._source_snapshot(), before)

        update = self._post_data(
            "счет елка",
            flow_type=CashFlowArticleMapping.FLOW_OPERATING,
            status=CashFlowArticleMapping.CLASS_CONFIRMED,
            category="Основные расходы",
            confirmation="on",
            comment="Уточнено",
        )
        self.client.post(save_url, update)
        mapping.refresh_from_db()
        self.assertEqual(mapping.management_category, "Основные расходы")
        self.assertEqual(mapping.comment, "Уточнено")
        self.assertEqual(CashFlowArticleMapping.objects.count(), 1)
        self.assertEqual(DataAuditLog.objects.filter(
            entity_type="CashFlowArticleMapping", entity_id=str(mapping.pk)
        ).count(), 2)

    def test_unconfirmed_mapping_stays_external_unclassified(self):
        self._row("Требует решения", "9.00", "4.00", number=1)
        response = self.client.post(
            reverse("finance_onec_cashflow_mapping_save"),
            self._post_data(
                "требует решения",
                flow_type=CashFlowArticleMapping.FLOW_OPERATING,
                status=CashFlowArticleMapping.CLASS_NEEDS_REVIEW,
                category="Операционная деятельность",
            ),
        )
        self.assertEqual(response.status_code, 302)
        mapping = CashFlowArticleMapping.objects.get()
        self.assertEqual(mapping.classification_status, CashFlowArticleMapping.CLASS_NEEDS_REVIEW)
        data = management_cashflow_data(self.organization, self.month, self.month)
        article = data["articles"][0]
        self.assertEqual(article["flow_type"], CashFlowArticleMapping.FLOW_UNCLASSIFIED)
        self.assertEqual(article["allocation"], "external")
        registry = data["mapping_review_registry"][0]
        self.assertIn("classification_not_confirmed", registry["reasons"])

    def test_internal_and_liquidity_rules_share_service_dashboard_and_contract_totals(self):
        self._row("Перемещение", "70.00", "10.00", number=1)
        self._row("Депозит", "90.00", "30.00", number=2)
        self.assertNotIn(
            CashFlowArticleMapping.FLOW_LIQUIDITY,
            dict(CashFlowArticleMapping.FLOW_CHOICES),
        )
        save_url = reverse("finance_onec_cashflow_mapping_save")
        for article, flow_type, category in (
            ("перемещение", CashFlowArticleMapping.FLOW_INTERNAL, "Перемещения"),
            ("депозит", CashFlowArticleMapping.FLOW_LIQUIDITY, "Депозиты"),
        ):
            response = self.client.post(save_url, self._post_data(
                article,
                flow_type=flow_type,
                status=CashFlowArticleMapping.CLASS_CONFIRMED,
                category=category,
                confirmation="on",
            ))
            self.assertEqual(response.status_code, 302)

        internal = CashFlowArticleMapping.objects.get(
            normalized_article_name="перемещение"
        )
        liquidity = CashFlowArticleMapping.objects.get(
            normalized_article_name="депозит"
        )
        self.assertTrue(internal.is_internal_turnover)
        self.assertFalse(internal.include_in_external_cashflow)
        self.assertFalse(liquidity.is_internal_turnover)
        self.assertTrue(liquidity.include_in_external_cashflow)

        expected = management_cashflow_data(self.organization, self.month, self.month)
        self.assertEqual(expected["internal"]["net_cash_flow"], Decimal("60.00"))
        self.assertEqual(expected["liquidity"]["net_cash_flow"], Decimal("60.00"))
        self.assertEqual(expected["external"]["net_cash_flow"], Decimal("60.00"))
        self.assertEqual(expected["operating"]["net_cash_flow"], Decimal("0.00"))

        dashboard = self.client.get(reverse("finance_onec_cashflow_dashboard"), {
            "period_from": "2026-01", "period_to": "2026-01",
        })
        self.assertEqual(dashboard.context["internal"], expected["internal"])
        self.assertEqual(dashboard.context["liquidity"], expected["liquidity"])
        contract = get_cashflow_breakdown(self.organization, self.month, self.month)
        self.assertEqual(contract["totals"]["internal"]["net_cash_flow"], "60.00")
        self.assertEqual(contract["totals"]["liquidity"]["net_cash_flow"], "60.00")
        self.assertEqual(contract["totals"]["external"]["net_cash_flow"], "60.00")

    def test_confirmed_financing_dividend_is_audited_prefilled_and_read_only_visible(self):
        self._row("Александр", "0.00", "100.00", number=1)
        before = self._source_snapshot()
        response = self.client.post(
            reverse("finance_onec_cashflow_mapping_save"),
            self._post_data(
                "александр",
                flow_type=CashFlowArticleMapping.FLOW_FINANCING,
                status=CashFlowArticleMapping.CLASS_CONFIRMED,
                category="Дивиденды / выплаты собственнику",
                confirmation="on",
                is_dividend="on",
            ),
        )
        self.assertEqual(response.status_code, 302)
        mapping = CashFlowArticleMapping.objects.get()
        self.assertTrue(mapping.is_dividend)
        self.assertEqual(mapping.flow_type, CashFlowArticleMapping.FLOW_FINANCING)
        self.assertTrue(mapping.include_in_external_cashflow)
        self.assertFalse(mapping.is_internal_turnover)
        audit = DataAuditLog.objects.get(
            entity_type="CashFlowArticleMapping", entity_id=str(mapping.pk)
        )
        self.assertTrue(audit.after["is_dividend"])
        self.assertIn("is_dividend", audit.changed_fields)
        self.assertEqual(self._source_snapshot(), before)

        data = management_cashflow_data(self.organization, self.month, self.month)
        article = data["articles"][0]
        self.assertTrue(article["is_dividend"])
        self.assertEqual(data["financing"]["payments"], Decimal("100.00"))
        self.assertEqual(data["financing"]["net_cash_flow"], Decimal("-100.00"))
        self.assertEqual(data["external"]["net_cash_flow"], Decimal("-100.00"))

        contract = get_cashflow_breakdown(
            self.organization, self.month, self.month, group_by="article"
        )
        self.assertTrue(contract["grouped"][0]["is_dividend"])
        response = self.client.get(reverse("finance_onec_cashflow_mapping"), {
            "period_from": "2026-01", "period_to": "2026-01",
        })
        self.assertRegex(
            response.content.decode(),
            r'<input[^>]+name="is_dividend"[^>]+checked',
        )

    def test_dividend_flag_requires_confirmed_financing_in_form_and_service(self):
        self._row("Статья", "10.00", "0.00", number=1)
        before = self._source_snapshot()
        response = self.client.post(
            reverse("finance_onec_cashflow_mapping_save"),
            self._post_data(
                "статья",
                flow_type=CashFlowArticleMapping.FLOW_OPERATING,
                status=CashFlowArticleMapping.CLASS_CONFIRMED,
                confirmation="on",
                is_dividend="on",
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(CashFlowArticleMapping.objects.exists())
        self.assertEqual(self._source_snapshot(), before)

        with self.assertRaises(ValidationError):
            save_explicit_cashflow_mapping(
                organization=self.organization,
                article_name="Статья",
                expected_normalized_article_name="статья",
                values={
                    "flow_type": CashFlowArticleMapping.FLOW_FINANCING,
                    "classification_status": CashFlowArticleMapping.CLASS_NEEDS_REVIEW,
                    "management_category": "Дивиденды / выплаты собственнику",
                    "comment": "",
                    "is_dividend": True,
                },
                user=self.owner,
            )
        self.assertFalse(CashFlowArticleMapping.objects.exists())
        self.assertEqual(self._source_snapshot(), before)

    def test_operating_category_never_reverses_positive_receipt_direction(self):
        self._row("Прочие поступления ДС", "52170.00", "0.00", number=1)
        response = self.client.post(
            reverse("finance_onec_cashflow_mapping_save"),
            self._post_data(
                "прочие поступления дс",
                flow_type=CashFlowArticleMapping.FLOW_OPERATING,
                status=CashFlowArticleMapping.CLASS_CONFIRMED,
                category="Прочие мелкие операционные расходы",
                confirmation="on",
            ),
        )
        self.assertEqual(response.status_code, 302)
        data = management_cashflow_data(self.organization, self.month, self.month)
        article = data["articles"][0]
        self.assertEqual(article["category"], "Прочие мелкие операционные расходы")
        self.assertEqual(article["receipts"], Decimal("52170.00"))
        self.assertEqual(article["payments"], Decimal("0.00"))
        self.assertEqual(data["operating"]["receipts"], Decimal("52170.00"))
        self.assertEqual(data["operating"]["payments"], Decimal("0.00"))
        self.assertEqual(data["operating"]["net_cash_flow"], Decimal("52170.00"))

    def test_non_authoritative_dividend_flag_is_not_exposed_or_reclassified(self):
        self._row("Старая статья", "10.00", "4.00", number=1)
        CashFlowArticleMapping.objects.create(
            organization=self.organization,
            article_name="Старая статья",
            normalized_article_name="старая статья",
            management_category="Операционные расходы",
            flow_type=CashFlowArticleMapping.FLOW_OPERATING,
            classification_status=CashFlowArticleMapping.CLASS_CONFIRMED,
            is_dividend=True,
            include_in_external_cashflow=True,
        )

        data = management_cashflow_data(self.organization, self.month, self.month)
        article = data["articles"][0]
        self.assertFalse(article["is_dividend"])
        self.assertEqual(article["flow_type"], CashFlowArticleMapping.FLOW_OPERATING)
        self.assertEqual(article["net_cash_flow"], Decimal("6.00"))
        self.assertEqual(data["operating"]["net_cash_flow"], Decimal("6.00"))
        registry = data["mapping_review_registry"]
        self.assertIn(
            "dividend_flag_requires_confirmed_financing", registry[0]["reasons"]
        )
        response = self.client.get(reverse("finance_onec_cashflow_mapping"), {
            "period_from": "2026-01", "period_to": "2026-01",
        })
        self.assertNotRegex(
            response.content.decode(),
            r'<input[^>]+name="is_dividend"[^>]+checked',
        )

    def test_source_preview_is_active_same_org_bounded_and_excludes_source_data(self):
        article = "Прочие поступления ДС"
        self._row(
            article, "52170.00", "0.00", number=1,
            document_raw="Активная аналитика", source_reference="Оплата по договору",
            source_data={"private": "ACTIVE-SECRET"},
        )
        self._row(
            article, "1.00", "2.00", number=2,
            document_raw="Вторая активная аналитика", source_reference="REF-B",
            source_data={"private": "SECOND-SECRET"},
        )
        historical = self._batch(self.organization, "historical-source")
        self._row(
            article, "999.00", "0.00", number=1, batch=historical,
            document_raw="Историческая аналитика", source_data={"private": "OLD-SECRET"},
        )
        preview_batch = self._batch(
            self.organization, "preview-source", status=OneCImportBatch.STATUS_PREVIEWED
        )
        preview_month = date(2026, 2, 1)
        self._activate(self.organization, preview_batch, preview_month)
        self._row(
            article, "888.00", "0.00", number=1, batch=preview_batch,
            month=preview_month,
            document_raw="Черновая аналитика", source_data={"private": "DRAFT-SECRET"},
        )
        foreign = self._batch(self.other_organization, "foreign-source")
        self._activate(self.other_organization, foreign, self.month)
        self._row(
            article, "777.00", "0.00", number=1, batch=foreign,
            organization=self.other_organization, document_raw="Чужая аналитика",
            source_data={"private": "FOREIGN-SECRET"},
        )

        previews = cashflow_article_source_previews(
            self.organization, ["прочие поступления дс"], self.month, preview_month
        )
        preview = previews["прочие поступления дс"]
        self.assertEqual(preview["row_count"], 2)
        self.assertEqual(len(preview["items"]), 2)
        self.assertFalse(preview["truncated"])
        self.assertEqual(preview["items"][0]["document_display"], "Активная аналитика")
        self.assertEqual(
            preview["items"][0]["source_reference_display"], "Оплата по договору"
        )
        self.assertEqual(preview["items"][0]["receipts"], Decimal("52170.00"))
        self.assertEqual(preview["items"][0]["payments"], Decimal("0.00"))
        self.assertEqual(preview["items"][0]["net_cash_flow"], Decimal("52170.00"))
        for item in preview["items"]:
            self.assertEqual(
                set(item),
                {
                    "period_month", "document_display", "source_reference_display",
                    "receipts", "payments", "net_cash_flow",
                },
            )
        bounded = cashflow_article_source_previews(
            self.organization, ["прочие поступления дс"], self.month, preview_month,
            limit=1,
        )["прочие поступления дс"]
        self.assertEqual(bounded["row_count"], 2)
        self.assertEqual(len(bounded["items"]), 1)
        self.assertTrue(bounded["truncated"])

        response = self.client.get(reverse("finance_onec_cashflow_mapping"), {
            "period_from": "2026-01", "period_to": "2026-01",
        })
        self.assertContains(response, "Активная аналитика")
        self.assertContains(response, "Вторая активная аналитика")
        for forbidden in (
            "Историческая аналитика", "Черновая аналитика", "Чужая аналитика",
            "REF-B", "ACTIVE-SECRET", "SECOND-SECRET", "OLD-SECRET",
            "DRAFT-SECRET", "FOREIGN-SECRET",
        ):
            self.assertNotContains(response, forbidden)
        self.assertLessEqual(len(preview["items"]), CASHFLOW_SOURCE_PREVIEW_LIMIT)

    def test_source_preview_suppresses_technical_payloads_and_keeps_clean_human_display(self):
        article = "Прочие поступления ДС"
        uuid = "123e4567-e89b-12d3-a456-426614174000"
        json_payload = '{"recorder": "Поступление от покупателя"}'
        recorder_payload = "Recorder: Поступление от покупателя"
        odata_dot_payload = "Document.РасходИзКассы"
        odata_payload = "StandardODATA.Document_РасходИзКассы"
        ref_key_payload = f"Ref_Key={uuid}"
        html_payload = "<strong>Поступление от покупателя</strong>"

        for unsafe_value in (
            json_payload,
            recorder_payload,
            odata_dot_payload,
            odata_payload,
            ref_key_payload,
            uuid,
            html_payload,
        ):
            self.assertEqual(_safe_cashflow_source_display(unsafe_value), "")
        self.assertEqual(
            _safe_cashflow_source_display("Безопасная аналитика"),
            "Безопасная аналитика",
        )
        self.assertEqual(
            _safe_cashflow_source_display("Договор поставки"),
            "Договор поставки",
        )

        self._row(
            article, "10.00", "0.00", number=1,
            document_raw=json_payload,
            source_reference="Договор для JSON",
        )
        self._row(
            article, "20.00", "0.00", number=2,
            document_raw="Безопасная аналитика",
            source_reference=recorder_payload,
        )
        self._row(
            article, "30.00", "0.00", number=3,
            document_raw=odata_dot_payload,
            source_reference=ref_key_payload,
        )
        self._row(
            article, "40.00", "0.00", number=4,
            document_raw=odata_payload,
            source_reference=html_payload,
        )
        self._row(
            article, "50.00", "0.00", number=5,
            document_raw=uuid,
        )
        self._row(
            article, "60.00", "0.00", number=6,
            document_raw="Чистая строка",
            source_reference="Договор поставки",
        )

        preview = cashflow_article_source_previews(
            self.organization, ["прочие поступления дс"], self.month, self.month
        )["прочие поступления дс"]
        self.assertEqual(len(preview["items"]), 6)
        displayed_pairs = {
            (item["document_display"], item["source_reference_display"])
            for item in preview["items"]
        }
        self.assertIn(("Чистая строка", "Договор поставки"), displayed_pairs)
        self.assertIn(("Безопасная аналитика", ""), displayed_pairs)
        self.assertIn(("", "Договор для JSON"), displayed_pairs)
        self.assertEqual(
            sum(not item["document_display"] for item in preview["items"]), 4
        )
        self.assertEqual(
            sum(not item["source_reference_display"] for item in preview["items"]), 4
        )
        rendered_preview = repr(preview)
        for forbidden in (
            json_payload,
            recorder_payload,
            odata_dot_payload,
            odata_payload,
            ref_key_payload,
            uuid,
            html_payload,
            "Поступление от покупателя",
            "Recorder",
            "Ref_Key",
            "StandardODATA",
            "Document.РасходИзКассы",
        ):
            self.assertNotIn(forbidden, rendered_preview)

        response = self.client.get(reverse("finance_onec_cashflow_mapping"), {
            "period_from": "2026-01", "period_to": "2026-01",
        })
        self.assertContains(response, "Чистая строка")
        self.assertContains(response, "Договор поставки")
        self.assertContains(response, "Безопасная аналитика")
        self.assertContains(response, "Договор для JSON")
        for forbidden in (
            json_payload,
            recorder_payload,
            odata_dot_payload,
            odata_payload,
            ref_key_payload,
            uuid,
            html_payload,
            "Поступление от покупателя",
            "Recorder",
            "Ref_Key",
            "StandardODATA",
            "Document.РасходИзКассы",
        ):
            self.assertNotContains(response, forbidden)

    def test_post_never_creates_mapping_for_inactive_or_other_organization_article(self):
        self._row("Своя статья", "10.00", "1.00", number=1)
        foreign = self._batch(self.other_organization, "foreign-active")
        self._activate(self.other_organization, foreign, self.month)
        self._row(
            "Чужая статья", "50.00", "1.00", number=1, batch=foreign,
            organization=self.other_organization,
        )
        response = self.client.post(
            reverse("finance_onec_cashflow_mapping_save"),
            self._post_data(
                "чужая статья",
                flow_type=CashFlowArticleMapping.FLOW_OPERATING,
                status=CashFlowArticleMapping.CLASS_CONFIRMED,
                confirmation="on",
            ),
        )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(CashFlowArticleMapping.objects.exists())
