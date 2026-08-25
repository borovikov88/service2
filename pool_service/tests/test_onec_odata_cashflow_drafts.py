from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
import importlib
import inspect
import json
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.exceptions import ValidationError
from django.core.management import call_command, CommandError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.odata_cashflow import read_cashflow_rows
from pool_service.finance_imports.odata_cashflow_drafts import (
    NO_ARTICLE_LABEL,
    ODataCashFlowDraftError,
    confirm_odata_cashflow,
    create_odata_cashflow_draft,
)
from pool_service.finance_imports.odata_profit import ODataConfig, ODataPreviewError
from pool_service.models import (
    CashFlowRow,
    DataAuditLog,
    OneCImportBatch,
    OneCReportPeriodActivation,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
)
from pool_service.tests.test_onec_odata_profit_preview import FakeOpener


ORG = "11111111-1111-1111-1111-111111111111"
OTHER_ORG = "22222222-2222-2222-2222-222222222222"
ARTICLE = "33333333-3333-3333-3333-333333333333"
BASE_URL = "https://fresh.example/odata/standard.odata/"


def config(**overrides):
    values = {
        "base_url": BASE_URL,
        "organization_guids": (ORG,),
        "timeout_seconds": 7,
        "max_pages": 10,
        "max_rows": 1000,
    }
    values.update(overrides)
    return ODataConfig(**values)


def cashflow_row(
    line=1, *, recorder="Document.Ref.123", recorder_type="StandardODATA.Document_x",
    period="2026-05-15T10:00:00+03:00", active=True, organization=ORG,
    article=ARTICLE, receipts="100.00", payments="40.00",
):
    return {
        "Recorder": recorder,
        "Recorder_Type": recorder_type,
        "LineNumber": line,
        "Period": period,
        "Active": active,
        "Организация_Key": organization,
        "ТипДенежныхСредств": "Безналичные",
        "БанковскийСчетКасса": "Расчётный счёт",
        "Валюта_Key": "44444444-4444-4444-4444-444444444444",
        "Статья_Key": article,
        "ХозяйственнаяОперация_Key": "55555555-5555-5555-5555-555555555555",
        "Проект_Key": "00000000-0000-0000-0000-000000000000",
        "Подразделение_Key": "66666666-6666-6666-6666-666666666666",
        "Аналитика": "Оплата покупателя",
        "СуммаПриход": receipts,
        "СуммаРасход": payments,
    }


def article_payload(
    *, description="Оплата от покупателей", deletion=False, invalid=False,
    include=True,
):
    return {"value": ([{
        "Ref_Key": ARTICLE,
        "Description": description,
        "DeletionMark": deletion,
        "Недействителен": invalid,
    }] if include else [])}


class ODataCashFlowReaderTests(SimpleTestCase):
    def read(self, payloads, *, cfg=None):
        opener = FakeOpener(*payloads)
        rows, pages = read_cashflow_rows(
            cfg or config(), "2026-05", "2026-05", opener=opener
        )
        return rows, pages, opener

    def test_raw_system_query_options_and_filter_contract(self):
        rows, pages, opener = self.read([{"value": [cashflow_row()]}])
        self.assertEqual((len(rows), pages), (1, 1))
        request = opener.requests[0][0]
        self.assertEqual(request.get_method(), "GET")
        raw_query = urlsplit(request.full_url).query
        self.assertIn("$select=", raw_query)
        self.assertIn("&$filter=", raw_query)
        self.assertNotIn("%24", raw_query)
        self.assertNotIn("+", raw_query)
        self.assertIn("%20", raw_query)
        query = parse_qs(raw_query)
        self.assertIn("Recorder_Type", query["$select"][0])
        self.assertIn("СуммаПриход", query["$select"][0])
        filter_value = query["$filter"][0]
        self.assertIn("Active eq true", filter_value)
        self.assertIn("Period ge datetime'2026-05-01T00:00:00'", filter_value)
        self.assertIn("Period lt datetime'2026-06-01T00:00:00'", filter_value)
        self.assertIn(ORG, filter_value)
        self.assertIn("AccumulationRegister_ДвиженияДенежныхСредств", unquote(request.full_url))

    def test_source_calendar_date_guard_and_true_out_of_range(self):
        rows, _, _ = self.read([{"value": [cashflow_row(
            period="2026-05-01T00:30:00+03:00"
        )]}])
        self.assertEqual(len(rows), 1)
        for period in ("2025-05-05T00:00:00", "2026-06-01T00:00:00+03:00"):
            with self.subTest(period=period), self.assertRaisesRegex(
                ODataPreviewError, "outside the requested month"
            ):
                self.read([{"value": [cashflow_row(period=period)]}])

    def test_inactive_is_ignored_and_non_allowlisted_is_rejected(self):
        rows, _, _ = self.read([{"value": [cashflow_row(active=False)]}])
        self.assertEqual(rows, [])
        with self.assertRaisesRegex(ODataPreviewError, "allowlist"):
            self.read([{"value": [cashflow_row(organization=OTHER_ORG)]}])

    def test_identity_includes_recorder_type_and_rejects_exact_duplicate(self):
        rows, _, _ = self.read([{"value": [
            cashflow_row(recorder_type="TypeA"),
            cashflow_row(recorder_type="TypeB"),
        ]}])
        self.assertEqual(len(rows), 2)
        with self.assertRaisesRegex(ODataPreviewError, "Duplicate"):
            self.read([{"value": [cashflow_row(), cashflow_row()]}])

    def test_decimal_net_and_invalid_amounts(self):
        rows, _, _ = self.read([{"value": [cashflow_row(
            receipts="100.10", payments="40.05"
        )]}])
        self.assertEqual(rows[0].net_cash_flow, Decimal("60.05"))
        for field, value in (("receipts", "NaN"), ("payments", "Infinity"), ("receipts", "-1")):
            kwargs = {field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ODataPreviewError):
                self.read([{"value": [cashflow_row(**kwargs)]}])

    def test_money_rejects_more_than_two_decimal_places(self):
        for field in ("receipts", "payments"):
            with self.subTest(field=field), self.assertRaisesRegex(
                ODataPreviewError, "at most two decimal places"
            ):
                self.read([{"value": [cashflow_row(**{field: "1.001"})]}])

    def test_money_accepts_trailing_zero_precision(self):
        rows, _, _ = self.read([{"value": [
            cashflow_row(1, receipts="100.000", payments="40.000"),
            cashflow_row(2, receipts="0.000", payments="0.000"),
        ]}])
        self.assertEqual(rows[0].receipts, Decimal("100.000"))
        self.assertEqual(rows[0].payments, Decimal("40.000"))
        self.assertEqual(rows[0].net_cash_flow, Decimal("60.000"))
        self.assertEqual(rows[1].net_cash_flow, Decimal("0.000"))

    def test_pagination_loop_cross_origin_and_limits(self):
        next_url = BASE_URL + "AccumulationRegister_x?$skiptoken=2"
        rows, pages, _ = self.read([
            {"value": [cashflow_row(1)], "@odata.nextLink": next_url},
            {"d": {"results": [cashflow_row(2)]}},
        ])
        self.assertEqual((len(rows), pages), (2, 2))
        with self.assertRaisesRegex(ODataPreviewError, "loop"):
            self.read([
                {"value": [cashflow_row(1)], "@odata.nextLink": next_url},
                {"value": [cashflow_row(2)], "@odata.nextLink": next_url},
            ])
        with self.assertRaisesRegex(ODataPreviewError, "origin"):
            self.read([{"value": [], "@odata.nextLink": "https://evil.example/x"}])
        with self.assertRaisesRegex(ODataPreviewError, "row limit"):
            self.read([{"value": [cashflow_row(1), cashflow_row(2)]}], cfg=config(max_rows=1))
        with self.assertRaisesRegex(ODataPreviewError, "page limit"):
            self.read([{"value": [], "@odata.nextLink": next_url}], cfg=config(max_pages=1))


@override_settings(
    ONEC_ODATA_BASE_URL=BASE_URL,
    ONEC_ODATA_USERNAME="",
    ONEC_ODATA_PASSWORD="",
    ONEC_ODATA_ORGANIZATION_GUIDS=(ORG,),
    ONEC_ODATA_TIMEOUT_SECONDS="5",
    ONEC_ODATA_MAX_PAGES="2",
    ONEC_ODATA_MAX_ROWS="100",
)
class ODataCashFlowCommandTests(TestCase):
    def test_command_uses_register_get_only_and_writes_nothing(self):
        opener = FakeOpener({"value": [
            cashflow_row(1, receipts="100.25", payments="40.10"),
            cashflow_row(2, receipts="20.00", payments="5.05"),
            cashflow_row(3, active=False),
        ]})
        before = (
            OneCImportBatch.objects.count(),
            CashFlowRow.objects.count(),
            OneCReportPeriodState.objects.count(),
            OneCReportPeriodActivation.objects.count(),
            DataAuditLog.objects.count(),
        )
        stdout = StringIO()
        with patch(
            "pool_service.finance_imports.odata_profit.build_opener",
            return_value=opener,
        ):
            call_command(
                "onec_odata_cashflow_preview",
                start_month="2026-05",
                end_month="2026-05",
                stdout=stdout,
            )
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(opener.requests[0][0].get_method(), "GET")
        self.assertEqual(before, (
            OneCImportBatch.objects.count(),
            CashFlowRow.objects.count(),
            OneCReportPeriodState.objects.count(),
            OneCReportPeriodActivation.objects.count(),
            DataAuditLog.objects.count(),
        ))
        output = json.loads(stdout.getvalue())
        self.assertEqual(output, {"total": {
            "row_count": 2,
            "page_count": 1,
            "receipts": "120.25",
            "payments": "45.15",
            "net_cash_flow": "75.10",
        }})
        for forbidden in (BASE_URL, ORG, ARTICLE, "Оплата", "Document"):
            self.assertNotIn(forbidden, stdout.getvalue())

    def test_safe_reader_error_is_exact_command_error(self):
        with patch(
            "pool_service.management.commands.onec_odata_cashflow_preview.read_cashflow_rows",
            side_effect=ODataPreviewError("safe cash-flow error"),
        ), self.assertRaisesMessage(CommandError, "safe cash-flow error"):
            call_command(
                "onec_odata_cashflow_preview",
                start_month="2026-05",
                end_month="2026-05",
            )


class ODataCashFlowDraftTests(TestCase):
    def setUp(self):
        self.private_dir = TemporaryDirectory()
        self.override = override_settings(
            PRIVATE_MEDIA_ROOT=self.private_dir.name,
            ONEC_ODATA_TARGET_ORGANIZATION_ID="1",
        )
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.private_dir.cleanup)
        self.organization = Organization.objects.create(
            id=1, name="OData cash flow",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.other = Organization.objects.create(name="Other")
        self.user = User.objects.create_user("cashflow-owner", password="test")
        OrganizationAccess.objects.create(
            user=self.user, organization=self.organization, role="owner"
        )
        self.client.force_login(self.user)

    def draft(self, rows=None, *, opener=None, start="2026-05", end="2026-05"):
        rows = [cashflow_row()] if rows is None else rows
        return create_odata_cashflow_draft(
            start, end, self.organization, self.user,
            config=config(), opener=opener or FakeOpener(
                {"value": rows}, article_payload()
            ),
        )

    def active_xlsx(self, month=date(2026, 5, 1)):
        batch = OneCImportBatch.objects.create(
            organization=self.organization,
            import_type=OneCImportBatch.TYPE_CASHFLOW,
            original_filename="cashflow.xlsx",
            stored_file="onec_imports/cashflow.xlsx",
            file_sha256=(f"{month:%Y%m}" * 11)[:64],
            status=OneCImportBatch.STATUS_CONFIRMED,
            uploaded_by=self.user,
            confirmed_by=self.user,
            confirmed_at=timezone.now(),
            period_first=month,
            period_last=month,
        )
        row = CashFlowRow.objects.create(
            import_batch=batch,
            organization=self.organization,
            period_month=month,
            source_row_number=6,
            article_raw="Старая статья",
            normalized_article_name="старая статья",
            document_raw="Старый документ",
            receipts=Decimal("80.00"), payments=Decimal("30.00"),
            net_cash_flow=Decimal("50.00"),
        )
        state = OneCReportPeriodState.objects.create(
            organization=self.organization,
            report_type=OneCImportBatch.TYPE_CASHFLOW,
            period_month=month,
            active_batch=batch,
            updated_by=self.user,
        )
        return batch, row, state

    def test_draft_snapshot_has_no_rows_or_activation_and_shows_differences(self):
        self.active_xlsx()
        batch = self.draft()
        self.assertEqual(batch.import_type, OneCImportBatch.TYPE_CASHFLOW)
        self.assertEqual(batch.source_type, OneCImportBatch.SOURCE_ODATA)
        self.assertEqual(batch.status, OneCImportBatch.STATUS_PREVIEWED)
        self.assertEqual(CashFlowRow.objects.count(), 1)
        self.assertEqual(OneCReportPeriodState.objects.count(), 1)
        item = batch.metadata["monthly"][0]
        self.assertEqual(item["net_cash_flow"], "60.00")
        self.assertEqual(item["active_net_cash_flow"], "50.00")
        self.assertEqual(item["net_cash_flow_difference"], "10.00")
        with batch.stored_file.open("rb") as source:
            snapshot = json.loads(source.read().decode())
        saved = snapshot["rows"][0]
        self.assertEqual(saved["article_raw"], "Оплата от покупателей")
        self.assertNotEqual(saved["article_raw"], ARTICLE)
        self.assertIn("recorder", saved["source_data"])

    def test_draft_accepts_money_with_trailing_zero_precision(self):
        row = cashflow_row(receipts="100.000", payments="40.000")
        batch = self.draft(
            rows=[row], opener=FakeOpener({"value": [row]}, article_payload())
        )
        self.assertEqual(batch.status, OneCImportBatch.STATUS_PREVIEWED)
        self.assertEqual(batch.metadata["totals"]["receipts"], "100.000")
        self.assertEqual(batch.metadata["totals"]["payments"], "40.000")
        self.assertFalse(CashFlowRow.objects.filter(import_batch=batch).exists())

    def test_article_lookup_is_bounded_get_and_requires_explicit_active_flags(self):
        opener = FakeOpener({"value": [cashflow_row()]}, article_payload())
        self.draft(opener=opener)
        self.assertEqual(len(opener.requests), 2)
        request = opener.requests[1][0]
        self.assertEqual(request.get_method(), "GET")
        raw = urlsplit(request.full_url).query
        self.assertIn("$select=Ref_Key%2CDescription%2CDeletionMark", raw)
        self.assertIn("%D0%9D%D0%B5%D0%B4%D0%B5%D0%B9", raw)
        self.assertIn("$filter=Ref_Key%20eq%20guid%27", raw)
        for payload in (
            article_payload(deletion=True),
            article_payload(invalid=True),
            article_payload(include=False),
            article_payload(description=""),
            article_payload(description=ARTICLE),
        ):
            with self.subTest(payload=payload), self.assertRaises(ODataCashFlowDraftError):
                self.draft(opener=FakeOpener({"value": [cashflow_row()]}, payload))

    def test_missing_article_keeps_amount_and_warning_without_catalog_get(self):
        opener = FakeOpener({"value": [cashflow_row(article=None)]})
        batch = self.draft(rows=[cashflow_row(article=None)], opener=opener)
        self.assertEqual(len(opener.requests), 1)
        self.assertEqual(batch.metadata["totals"]["net_cash_flow"], "60.00")
        self.assertTrue(batch.metadata["warnings"])
        self.assertEqual(batch.metadata["preview"][0]["article_raw"], NO_ARTICLE_LABEL)

    def test_confirm_is_atomic_and_activates_only_scope(self):
        old_batch, _, old_state = self.active_xlsx()
        june_batch, _, june_state = self.active_xlsx(date(2026, 6, 1))
        batch = self.draft()
        confirm_odata_cashflow(batch.id, self.organization, self.user, config=config())
        batch.refresh_from_db(); old_state.refresh_from_db(); june_state.refresh_from_db()
        self.assertEqual(batch.status, OneCImportBatch.STATUS_CONFIRMED)
        self.assertEqual(old_state.active_batch, batch)
        self.assertEqual(june_state.active_batch, june_batch)
        self.assertTrue(CashFlowRow.objects.filter(import_batch=old_batch).exists())
        self.assertTrue(OneCReportPeriodActivation.objects.filter(
            batch=batch, replaced_batch=old_batch
        ).exists())
        active = CashFlowRow.objects.active_for(
            self.organization, OneCImportBatch.TYPE_CASHFLOW
        )
        self.assertEqual(active.filter(period_month=date(2026, 5, 1)).count(), 1)

    def test_empty_scope_month_replaces_old_active_version_and_keeps_history(self):
        old_batch, old_row, state = self.active_xlsx()
        batch = self.draft(rows=[], opener=FakeOpener({"value": []}))
        self.assertEqual(batch.metadata["monthly"][0]["row_count"], 0)
        confirm_odata_cashflow(batch.id, self.organization, self.user, config=config())
        state.refresh_from_db()
        self.assertEqual(state.active_batch, batch)
        self.assertTrue(CashFlowRow.objects.filter(pk=old_row.pk, import_batch=old_batch).exists())
        self.assertFalse(CashFlowRow.objects.active_for(
            self.organization, OneCImportBatch.TYPE_CASHFLOW
        ).filter(period_month=date(2026, 5, 1)).exists())

    def test_confirmation_failure_rolls_back_rows_states_and_activation(self):
        old_batch, _, state = self.active_xlsx()
        batch = self.draft()
        audit_count = DataAuditLog.objects.filter(
            entity_type="OneCImportBatch", entity_id=str(batch.id)
        ).count()
        with patch(
            "pool_service.finance_imports.odata_cashflow_drafts._activate_period_states",
            side_effect=ValidationError("stop"),
        ), self.assertRaises(ValidationError):
            confirm_odata_cashflow(batch.id, self.organization, self.user, config=config())
        state.refresh_from_db(); batch.refresh_from_db()
        self.assertEqual(state.active_batch, old_batch)
        self.assertFalse(CashFlowRow.objects.filter(import_batch=batch).exists())
        self.assertFalse(OneCReportPeriodActivation.objects.filter(batch=batch).exists())
        self.assertEqual(batch.status, OneCImportBatch.STATUS_FAILED)
        failure_audit = DataAuditLog.objects.filter(
            entity_type="OneCImportBatch", entity_id=str(batch.id)
        ).order_by("id")
        self.assertEqual(failure_audit.count(), audit_count + 1)
        self.assertEqual(failure_audit.last().before, {"status": "previewed"})
        self.assertEqual(failure_audit.last().after, {"status": "failed"})

    def test_same_identity_db_constraint_and_recorder_type_distinguishes_rows(self):
        batch = self.draft(rows=[
            cashflow_row(recorder_type="TypeA"),
            cashflow_row(recorder_type="TypeB"),
        ], opener=FakeOpener(
            {"value": [
                cashflow_row(recorder_type="TypeA"),
                cashflow_row(recorder_type="TypeB"),
            ]}, article_payload()
        ))
        confirm_odata_cashflow(batch.id, self.organization, self.user, config=config())
        saved = list(CashFlowRow.objects.filter(import_batch=batch))
        self.assertEqual(len({row.source_identity for row in saved}), 2)
        duplicate = CashFlowRow(
            import_batch=batch, organization=self.organization,
            period_month=saved[0].period_month,
            source_identity=saved[0].source_identity,
            source_row_number=99, article_raw="x", normalized_article_name="x",
            document_raw="", receipts=0, payments=0, net_cash_flow=0,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CashFlowRow.objects.bulk_create([duplicate])

    def test_target_mismatch_fails_before_get_and_batch(self):
        opener = FakeOpener()
        with self.assertRaises(ODataCashFlowDraftError):
            create_odata_cashflow_draft(
                "2026-05", "2026-05", self.other, self.user,
                config=config(), opener=opener,
            )
        self.assertEqual(opener.requests, [])
        self.assertFalse(OneCImportBatch.objects.filter(organization=self.other).exists())

    def test_form_service_limit_permissions_post_and_dashboard_active_rows(self):
        opener = FakeOpener()
        with self.assertRaises(ODataCashFlowDraftError):
            create_odata_cashflow_draft(
                "2025-05", "2026-05", self.organization, self.user,
                config=config(), opener=opener,
            )
        self.assertEqual(opener.requests, [])
        url = reverse("finance_onec_odata_cashflow_draft")
        self.assertEqual(self.client.get(url).status_code, 405)
        outsider = User.objects.create_user("outsider", password="test")
        self.client.force_login(outsider)
        self.assertEqual(self.client.post(url, {
            "start_month": "2026-05", "end_month": "2026-05"
        }).status_code, 403)
        self.client.force_login(self.user)
        self.active_xlsx()
        response = self.client.get(reverse("finance_onec_cashflow_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["totals"]["net_cash_flow"], Decimal("50"))

    @patch("pool_service.finance_views.create_odata_cashflow_draft")
    def test_authorized_create_and_confirm_are_separate_posts(self, create_mock):
        batch = OneCImportBatch.objects.create(
            organization=self.organization,
            import_type=OneCImportBatch.TYPE_CASHFLOW,
            source_type=OneCImportBatch.SOURCE_ODATA,
            original_filename="draft.json",
            stored_file="onec_imports/draft.json",
            file_sha256="8" * 64,
            uploaded_by=self.user,
            status=OneCImportBatch.STATUS_PREVIEWED,
        )
        create_mock.return_value = batch
        create_url = reverse("finance_onec_odata_cashflow_draft")
        response = self.client.post(create_url, {
            "start_month": "2026-05", "end_month": "2026-05",
        })
        self.assertRedirects(
            response, reverse("finance_onec_cashflow_preview", args=[batch.id])
        )
        create_mock.assert_called_once_with(
            "2026-05", "2026-05", self.organization, self.user
        )
        confirm_url = reverse("finance_onec_cashflow_confirm", args=[batch.id])
        self.assertEqual(self.client.get(confirm_url).status_code, 405)
        with patch(
            "pool_service.finance_views.confirm_odata_cashflow", return_value=batch
        ) as confirm_mock:
            response = self.client.post(confirm_url)
        self.assertRedirects(response, reverse("finance_onec_cashflow_dashboard"))
        confirm_mock.assert_called_once_with(batch.id, self.organization, self.user)

    def test_target_list_shows_separate_cashflow_action(self):
        response = self.client.get(reverse("finance_onec_import_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Получить ДДС из 1С")

    def test_cashflow_only_list_hides_profit_batch_metadata(self):
        cashflow_batch, _, _ = self.active_xlsx()
        profit_batch = OneCImportBatch.objects.create(
            organization=self.organization,
            import_type=OneCImportBatch.TYPE_MONTHLY_PROFIT,
            original_filename="secret-profit.xlsx",
            stored_file="onec_imports/secret-profit.xlsx",
            file_sha256="a" * 64,
            uploaded_by=self.user,
        )
        with patch(
            "pool_service.finance_views.can_import_gross_profit", return_value=False
        ), patch(
            "pool_service.finance_views.can_import_cashflow", return_value=True
        ), patch(
            "pool_service.finance_views.can_view_cashflow", return_value=True
        ):
            response = self.client.get(reverse("finance_onec_import_list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, cashflow_batch.original_filename)
        self.assertNotContains(response, profit_batch.original_filename)

    def test_profit_routes_reject_cashflow_and_cashflow_routes_read_xlsx(self):
        batch, _, _ = self.active_xlsx()
        self.assertEqual(
            self.client.get(reverse("finance_onec_import_preview", args=[batch.id])).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse("finance_onec_import_detail", args=[batch.id])).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("finance_onec_import_confirm", args=[batch.id])).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(reverse("finance_onec_import_cancel", args=[batch.id])).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(reverse("finance_onec_cashflow_detail", args=[batch.id])).status_code,
            200,
        )

    def test_xlsx_cashflow_preview_and_confirm_use_foundation_service(self):
        batch = OneCImportBatch.objects.create(
            organization=self.organization,
            import_type=OneCImportBatch.TYPE_CASHFLOW,
            source_type=OneCImportBatch.SOURCE_XLSX,
            original_filename="cashflow-preview.xlsx",
            stored_file="onec_imports/cashflow-preview.xlsx",
            file_sha256="b" * 64,
            uploaded_by=self.user,
            status=OneCImportBatch.STATUS_PREVIEWED,
            metadata={
                "report": {
                    "month_count": 1,
                    "months": ["2026-05-01"],
                    "control_totals": {
                        "receipts": "10.00",
                        "payments": "4.00",
                        "net_cash_flow": "6.00",
                    },
                },
                "preview": [],
                "critical_errors": [],
            },
        )
        response = self.client.get(
            reverse("finance_onec_cashflow_preview", args=[batch.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "XLSX")
        self.assertContains(response, "10.00")
        with patch(
            "pool_service.finance_views.confirm_cashflow", return_value=batch
        ) as confirm_mock:
            response = self.client.post(
                reverse("finance_onec_cashflow_confirm", args=[batch.id])
            )
        self.assertRedirects(
            response, reverse("finance_onec_cashflow_detail", args=[batch.id])
        )
        confirm_mock.assert_called_once_with(batch.id, self.organization, self.user)

    def test_odata_draft_cancel_deletes_snapshot_and_audits_without_facts(self):
        batch = self.draft()
        storage = batch.stored_file.storage
        stored_name = batch.stored_file.name
        audit_count = DataAuditLog.objects.filter(
            entity_type="OneCImportBatch", entity_id=str(batch.id)
        ).count()
        cancel_url = reverse("finance_onec_cashflow_cancel", args=[batch.id])
        self.assertEqual(self.client.get(cancel_url).status_code, 405)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(cancel_url)
        self.assertRedirects(response, reverse("finance_onec_import_list"))
        batch.refresh_from_db()
        self.assertEqual(batch.status, OneCImportBatch.STATUS_CANCELLED)
        self.assertFalse(storage.exists(stored_name))
        self.assertFalse(CashFlowRow.objects.filter(import_batch=batch).exists())
        self.assertFalse(OneCReportPeriodActivation.objects.filter(batch=batch).exists())
        audits = DataAuditLog.objects.filter(
            entity_type="OneCImportBatch", entity_id=str(batch.id)
        ).order_by("id")
        self.assertEqual(audits.count(), audit_count + 1)
        self.assertEqual(audits.last().before, {"status": "previewed"})
        self.assertEqual(audits.last().after, {"status": "cancelled"})

    def test_xlsx_cashflow_preview_cancel_uses_same_safe_helper(self):
        batch = OneCImportBatch(
            organization=self.organization,
            import_type=OneCImportBatch.TYPE_CASHFLOW,
            source_type=OneCImportBatch.SOURCE_XLSX,
            original_filename="cashflow-cancel.xlsx",
            file_sha256="c" * 64,
            uploaded_by=self.user,
            status=OneCImportBatch.STATUS_PREVIEWED,
        )
        batch.stored_file.save("source.xlsx", ContentFile(b"xlsx"), save=False)
        batch.save()
        storage = batch.stored_file.storage
        stored_name = batch.stored_file.name
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("finance_onec_cashflow_cancel", args=[batch.id])
            )
        self.assertRedirects(response, reverse("finance_onec_import_list"))
        batch.refresh_from_db()
        self.assertEqual(batch.status, OneCImportBatch.STATUS_CANCELLED)
        self.assertFalse(storage.exists(stored_name))
        audit = DataAuditLog.objects.get(
            entity_type="OneCImportBatch", entity_id=str(batch.id)
        )
        self.assertEqual(audit.before, {"status": "previewed"})
        self.assertEqual(audit.after, {"status": "cancelled"})

    def test_confirmed_cashflow_cannot_be_cancelled_and_has_no_button(self):
        batch, _, _ = self.active_xlsx()
        cancel_url = reverse("finance_onec_cashflow_cancel", args=[batch.id])
        audit_count = DataAuditLog.objects.filter(
            entity_type="OneCImportBatch", entity_id=str(batch.id)
        ).count()
        response = self.client.get(
            reverse("finance_onec_cashflow_detail", args=[batch.id])
        )
        self.assertNotContains(response, f'action="{cancel_url}"')
        response = self.client.post(cancel_url)
        self.assertRedirects(response, reverse("finance_onec_import_list"))
        batch.refresh_from_db()
        self.assertEqual(batch.status, OneCImportBatch.STATUS_CONFIRMED)
        self.assertEqual(DataAuditLog.objects.filter(
            entity_type="OneCImportBatch", entity_id=str(batch.id)
        ).count(), audit_count)

    def test_preview_and_failed_show_cancel_but_cancelled_does_not(self):
        batch = self.draft()
        cancel_url = reverse("finance_onec_cashflow_cancel", args=[batch.id])
        response = self.client.get(
            reverse("finance_onec_cashflow_preview", args=[batch.id])
        )
        self.assertContains(response, f'action="{cancel_url}"')
        batch.status = OneCImportBatch.STATUS_FAILED
        batch.save(update_fields=["status"])
        response = self.client.get(
            reverse("finance_onec_cashflow_preview", args=[batch.id])
        )
        self.assertContains(response, f'action="{cancel_url}"')
        batch.status = OneCImportBatch.STATUS_CANCELLED
        batch.save(update_fields=["status"])
        response = self.client.get(
            reverse("finance_onec_cashflow_detail", args=[batch.id])
        )
        self.assertNotContains(response, f'action="{cancel_url}"')

    def test_cashflow_server_guards_deny_user_without_capability(self):
        batch, _, _ = self.active_xlsx()
        outsider = User.objects.create_user("cashflow-no-access", password="test")
        OrganizationAccess.objects.create(
            user=outsider, organization=self.organization, role="installer"
        )
        self.client.force_login(outsider)
        self.assertEqual(
            self.client.get(reverse("finance_onec_cashflow_dashboard")).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(reverse("finance_onec_cashflow_detail", args=[batch.id])).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("finance_onec_cashflow_confirm", args=[batch.id])).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(reverse("finance_onec_cashflow_cancel", args=[batch.id])).status_code,
            403,
        )

    def test_xlsx_identity_is_period_and_row(self):
        batch, row, _ = self.active_xlsx()
        self.assertEqual(row.source_identity, "xlsx:2026-05-01:6")
        other = CashFlowRow.objects.create(
            import_batch=batch, organization=self.organization,
            period_month=date(2026, 6, 1), source_row_number=6,
            article_raw="x", normalized_article_name="x", document_raw="",
            receipts=0, payments=0, net_cash_flow=0,
        )
        self.assertEqual(other.source_identity, "xlsx:2026-06-01:6")


class ODataCashFlowIdentityMigrationTests(TransactionTestCase):
    migrate_from = [("pool_service", "0095_onec_odata_draft_fields")]
    migrate_to = [("pool_service", "0096_cashflow_odata_source_identity")]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        apps = self.executor.loader.project_state(self.migrate_from).apps
        Organization = apps.get_model("pool_service", "Organization")
        User = apps.get_model("auth", "User")
        Batch = apps.get_model("pool_service", "OneCImportBatch")
        CashFlow = apps.get_model("pool_service", "CashFlowRow")
        organization = Organization.objects.create(name="Cashflow migration")
        user = User.objects.create(username="cashflow-migration-owner")
        batch = Batch.objects.create(
            organization=organization,
            import_type="cashflow",
            original_filename="historical-cashflow.xlsx",
            stored_file="onec_imports/historical-cashflow.xlsx",
            file_sha256="9" * 64,
            uploaded_by=user,
        )
        CashFlow.objects.create(
            import_batch=batch,
            organization=organization,
            period_month=date(2026, 5, 1),
            source_row_number=17,
            article_raw="Historical",
            normalized_article_name="historical",
            document_raw="Document",
            receipts=Decimal("10.00"),
            payments=Decimal("3.00"),
            net_cash_flow=Decimal("7.00"),
        )

    def tearDown(self):
        MigrationExecutor(connection).migrate(
            MigrationExecutor(connection).loader.graph.leaf_nodes()
        )
        super().tearDown()

    def test_backfill_and_plain_unique_constraint(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        apps = self.executor.loader.project_state(self.migrate_to).apps
        CashFlow = apps.get_model("pool_service", "CashFlowRow")
        row = CashFlow.objects.get()
        self.assertEqual(row.source_identity, "xlsx:2026-05-01:17")
        constraint = next(
            item for item in CashFlow._meta.constraints
            if item.name == "unique_cashflow_batch_source_identity"
        )
        self.assertIsNone(constraint.condition)
        migration = importlib.import_module(
            "pool_service.migrations.0096_cashflow_odata_source_identity"
        )
        self.assertNotIn("condition=", inspect.getsource(migration))
