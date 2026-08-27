from datetime import date, timedelta
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import uuid
import threading
from unittest import skipUnless

from django.contrib.auth.models import User
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.odata_profit import ODataConfig
from pool_service.finance_imports.odata_unified_sync import (
    REPORT_CASHFLOW,
    REPORT_PROFIT,
    _confirmed_candidate,
    month_fingerprint,
    _claim_step,
    reactivate_confirmed_candidate,
    start_unified_sync,
    step_unified_sync,
)
from pool_service.models import (
    CashFlowRow,
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCODataSyncRun,
    OneCReportPeriodActivation,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
    cashflow_source_identity,
)


ORG_GUID = "11111111-1111-1111-1111-111111111111"
RECORDER = "55555555-5555-5555-5555-555555555555"


def config():
    return ODataConfig(
        base_url="https://fresh.example/odata/standard.odata/",
        organization_guids=(ORG_GUID,), timeout_seconds=7, max_pages=10, max_rows=1000,
    )


def profit_row(month="2025-05", revenue="100.00"):
    return {
        "period_month": f"{month}-01", "source_recorder": RECORDER,
        "source_row_number": 1, "source_identity": f"odata:{RECORDER}:1",
        "manager_name": "Ответственный", "customer_name": "Покупатель",
        "document_name": "Реализация 1", "nomenclature": "Товар",
        "article": "A-1", "nomenclature_type": "Запас", "quantity": "2.000000",
        "revenue": revenue, "cost": "40.00", "gross_profit": str(float(revenue) - 40),
        "calculated_cost": None, "cost_source": "actual", "cost_calculation_method": "",
        "cost_calculation_ratio": None, "analytical_gross_profit": str(float(revenue) - 40),
        "profitability_percent": "60.0000" if revenue == "100.00" else "66.6667",
        "source_data": {
            "source": "odata", "recorder": RECORDER, "line_number": 1,
            "period": f"{month}-15T07:00:00+00:00", "source_date": f"{month}-15",
            "organization_guid": ORG_GUID,
            "nomenclature_guid": "33333333-3333-3333-3333-333333333333",
            "nomenclature_type": "Запас",
            "customer_guid": "44444444-4444-4444-4444-444444444444",
            "responsible_guid": "66666666-6666-6666-6666-666666666666", "vat": "10.00",
        },
    }


def cashflow_row(month="2025-05", receipts="100.00"):
    identity = cashflow_source_identity(
        period_month=date.fromisoformat(f"{month}-01"), source_row_number=1,
        source_recorder="Поступление 1", source_recorder_type="Document",
    )
    return {
        "period_month": f"{month}-01", "source_row_number": 1,
        "source_identity": identity, "source_reference": "",
        "article_raw": "Оплата покупателей", "normalized_article_name": "оплата покупателей",
        "document_raw": "Поступление 1", "receipts": receipts, "payments": "40.00",
        "net_cash_flow": str(float(receipts) - 40),
        "source_data": {
            "source": "odata", "recorder": "Поступление 1", "recorder_type": "Document",
            "line_number": 1, "source_date": f"{month}-15", "organization_guid": ORG_GUID,
            "article_guid": "77777777-7777-7777-7777-777777777777",
            "cash_type": "", "account_or_cash": "", "currency_guid": "",
            "operation_guid": "", "project_guid": "", "department_guid": "",
            "analytics": "Поступление 1",
        },
    }


@override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=1)
class UnifiedSyncTests(TestCase):
    def setUp(self):
        self.private = TemporaryDirectory()
        self.override = override_settings(PRIVATE_MEDIA_ROOT=self.private.name)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.addCleanup(self.private.cleanup)
        self.organization = Organization.objects.create(
            id=1, name="Аквалайн", paid_until=timezone.now() + timedelta(days=30)
        )
        self.other = Organization.objects.create(name="Другая")
        self.user = User.objects.create_user("owner")
        OrganizationAccess.objects.create(
            organization=self.organization, user=self.user, role="owner"
        )

    def active_profit(self, month=date(2025, 5, 1), revenue="100.00"):
        batch = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA, original_filename="old.json",
            stored_file="old.json", file_sha256="1" * 64,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=self.user,
            period_first=month, period_last=month,
        )
        raw = profit_row(month.strftime("%Y-%m"), revenue)
        row = OneCMonthlyProfit.objects.create(
            import_batch=batch, organization=self.organization,
            period_month=month, source_recorder=RECORDER, source_row_number=1,
            manager_name=raw["manager_name"], customer_name=raw["customer_name"],
            document_name=raw["document_name"], nomenclature=raw["nomenclature"],
            article=raw["article"], nomenclature_type=raw["nomenclature_type"],
            quantity=raw["quantity"], revenue=raw["revenue"], cost=raw["cost"],
            gross_profit=raw["gross_profit"], cost_source="actual",
            analytical_gross_profit=raw["analytical_gross_profit"],
            profitability_percent=raw["profitability_percent"], source_data=raw["source_data"],
        )
        OneCReportPeriodState.objects.create(
            organization=self.organization, report_type=REPORT_PROFIT,
            period_month=month, active_batch=batch, updated_by=self.user,
        )
        return batch, row

    def run_profit(self, rows, *, today=date(2025, 5, 1)):
        run, _ = start_unified_sync(self.organization, self.user, [REPORT_PROFIT], today=today)
        with patch(
            "pool_service.finance_imports.odata_unified_sync._collect_profit_chunk",
            return_value=(rows, 1),
        ):
            return step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())

    def test_changed_old_month_creates_only_preview_without_activation(self):
        old, _ = self.active_profit()
        run = self.run_profit([profit_row(revenue="120.00")])
        self.assertEqual(run.status, OneCODataSyncRun.STATUS_COMPLETED)
        drafts = OneCImportBatch.objects.filter(status=OneCImportBatch.STATUS_PREVIEWED)
        self.assertEqual(drafts.count(), 1)
        self.assertEqual(drafts.get().period_first, date(2025, 5, 1))
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch, old)
        self.assertEqual(OneCMonthlyProfit.objects.filter(import_batch=drafts.get()).count(), 0)

    def test_unchanged_month_uses_historical_row_fingerprint_without_metadata(self):
        self.active_profit()
        historical = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA, original_filename="historical.json",
            stored_file="historical.json", file_sha256="2" * 64,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=self.user,
            period_first=date(2025, 5, 1), period_last=date(2025, 5, 1),
        )
        raw = profit_row(revenue="120.00")
        OneCMonthlyProfit.objects.create(
            import_batch=historical, organization=self.organization,
            period_month=date(2025, 5, 1), source_recorder=RECORDER,
            source_row_number=1, manager_name=raw["manager_name"],
            customer_name=raw["customer_name"], document_name=raw["document_name"],
            nomenclature=raw["nomenclature"], article=raw["article"],
            nomenclature_type=raw["nomenclature_type"], quantity=raw["quantity"],
            revenue=raw["revenue"], cost=raw["cost"], gross_profit=raw["gross_profit"],
            cost_source="actual", analytical_gross_profit=raw["analytical_gross_profit"],
            profitability_percent=raw["profitability_percent"], source_data=raw["source_data"],
        )
        run = self.run_profit([profit_row()])
        self.assertEqual(run.result_summary[REPORT_PROFIT]["unchanged_months"], ["2025-05-01"])
        self.assertFalse(OneCImportBatch.objects.filter(status="previewed").exists())

    def test_explicit_empty_month_creates_draft_and_does_not_confirm(self):
        old, old_row = self.active_profit()
        run = self.run_profit([])
        draft = OneCImportBatch.objects.get(status="previewed")
        self.assertEqual(draft.rows_detected, 0)
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch, old)
        self.assertTrue(OneCMonthlyProfit.objects.filter(pk=old_row.pk).exists())
        self.assertEqual(run.status, "completed")

    def test_active_empty_month_remains_unchanged_when_source_is_empty(self):
        month = date(2025, 5, 1)
        batch = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA, original_filename="empty.json",
            stored_file="empty.json", file_sha256="3" * 64,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=self.user,
            period_first=month, period_last=month,
        )
        OneCReportPeriodState.objects.create(
            organization=self.organization, report_type=REPORT_PROFIT,
            period_month=month, active_batch=batch, updated_by=self.user,
        )
        run = self.run_profit([])
        self.assertEqual(run.result_summary[REPORT_PROFIT]["unchanged_months"], ["2025-05-01"])
        self.assertFalse(OneCImportBatch.objects.filter(status="previewed").exists())

    def test_repeated_start_returns_existing_run_and_repeated_step_is_idempotent(self):
        run, created = start_unified_sync(self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1))
        same, second_created = start_unified_sync(self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1))
        self.assertTrue(created)
        self.assertFalse(second_created)
        self.assertEqual(same.pk, run.pk)
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([], 1)):
            finished = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())
            repeated = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())
        self.assertEqual(repeated.pk, finished.pk)
        self.assertEqual(OneCImportBatch.objects.filter(status="previewed").count(), 0)

    def test_report_types_are_independent_and_error_is_partial(self):
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], today=date(2025, 5, 1)
        )
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([], 1)):
            first = step_unified_sync(run.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], 0, config=config())
        with patch("pool_service.finance_imports.odata_unified_sync._collect_cashflow_chunk", side_effect=RuntimeError("safe failure")):
            final = step_unified_sync(run.id, self.user, [REPORT_PROFIT, REPORT_CASHFLOW], first.cursor["version"], config=config())
        self.assertEqual(final.status, OneCODataSyncRun.STATUS_RUNNING)
        self.assertEqual(final.result_summary[REPORT_PROFIT]["status"], "completed")
        self.assertEqual(final.result_summary[REPORT_CASHFLOW]["status"], "retryable_error")
        self.assertEqual(final.cursor["version"], first.cursor["version"])
        self.assertFalse(OneCReportPeriodState.objects.exists())

    def test_cashflow_change_creates_only_cashflow_preview(self):
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_CASHFLOW], today=date(2025, 5, 1)
        )
        with patch(
            "pool_service.finance_imports.odata_unified_sync._collect_cashflow_chunk",
            return_value=([cashflow_row()], 1, []),
        ):
            final = step_unified_sync(run.id, self.user, [REPORT_CASHFLOW], 0, config=config())
        draft = OneCImportBatch.objects.get(status="previewed")
        self.assertEqual(final.status, "completed")
        self.assertEqual(draft.import_type, REPORT_CASHFLOW)
        self.assertFalse(OneCImportBatch.objects.filter(import_type=REPORT_PROFIT).exists())

    def test_scope_is_organization_isolated_and_initial_scope_is_twelve_months(self):
        run, _ = start_unified_sync(self.organization, self.user, [REPORT_CASHFLOW], today=date(2026, 8, 1))
        scope = run.sync_scope[REPORT_CASHFLOW]
        self.assertEqual((scope["start"], scope["end"]), ("2025-09-01", "2026-08-01"))
        self.assertTrue(scope["initial_import"])
        self.assertFalse(OneCODataSyncRun.objects.filter(organization=self.other).exists())

    def test_initial_import_creates_draft_only_for_nonempty_month(self):
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        with patch(
            "pool_service.finance_imports.odata_unified_sync._collect_profit_chunk",
            return_value=([profit_row()], 1),
        ):
            final = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())
        self.assertEqual(final.status, OneCODataSyncRun.STATUS_COMPLETED)
        drafts = OneCImportBatch.objects.filter(status="previewed")
        self.assertEqual(drafts.count(), 1)
        self.assertEqual(drafts.get().period_first, date(2025, 5, 1))

    def test_month_fingerprint_is_order_independent(self):
        one = cashflow_row()
        two = cashflow_row()
        two["source_identity"] = "odata:" + "b" * 64
        two["source_row_number"] = 2
        self.assertEqual(
            month_fingerprint(REPORT_CASHFLOW, date(2025, 5, 1), [one, two]),
            month_fingerprint(REPORT_CASHFLOW, date(2025, 5, 1), [two, one]),
        )

    def test_model_contains_persisted_state_machine_fields(self):
        fields = {field.name for field in OneCODataSyncRun._meta.fields}
        self.assertTrue({"sync_scope", "cursor", "progress", "result_summary", "error_message"} <= fields)

    def test_interrupted_multichunk_run_can_resume(self):
        self.active_profit(date(2024, 1, 1))
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        self.assertEqual(run.progress["total_chunks"], 2)
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([], 1)):
            first = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())
        self.assertEqual(first.status, OneCODataSyncRun.STATUS_RUNNING)
        resumed = OneCODataSyncRun.objects.get(pk=run.pk)
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([], 1)):
            final = step_unified_sync(
                resumed.id, self.user, [REPORT_PROFIT], resumed.cursor["version"], config=config()
            )
        self.assertEqual(final.status, OneCODataSyncRun.STATUS_COMPLETED)

    def test_ui_start_is_post_only_and_permissions_are_server_side(self):
        self.client.force_login(self.user)
        url = reverse("finance_onec_odata_sync_start")
        self.assertEqual(self.client.get(url).status_code, 405)
        with patch("pool_service.finance_views.start_unified_sync") as start:
            run = OneCODataSyncRun.objects.create(
                organization=self.organization, requested_by=self.user,
                requested_report_types=[REPORT_PROFIT], sync_scope={},
                cursor={"version": 0}, progress={}, result_summary={},
            )
            start.return_value = (run, True)
            response = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["run_id"], str(run.id))
        start.assert_called_once()

        with patch("pool_service.finance_views.start_unified_sync", return_value=(run, False)):
            response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("finance_onec_import_list"))

        denied = User.objects.create_user("denied")
        OrganizationAccess.objects.create(
            organization=self.organization, user=denied, role="dispatcher"
        )
        self.client.force_login(denied)
        self.assertEqual(self.client.post(url).status_code, 403)

    def test_main_ui_has_unified_action_and_no_manual_period_fields(self):
        self.client.force_login(self.user)
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        response = self.client.get(reverse("finance_onec_import_list"))
        self.assertContains(response, "Обновить данные из 1С")
        self.assertContains(response, "ФОТ в это обновление не входит")
        self.assertNotContains(response, 'type="month"')
        self.assertContains(
            response,
            f'action="{reverse("finance_onec_odata_sync_step", args=[run.id])}"',
        )
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'name="cursor" value="0"')
        self.assertContains(response, 'type="submit" class="btn btn-sm btn-outline-primary"')
        self.assertContains(response, 'form.addEventListener("submit"')

    def test_plain_html_start_creates_run_redirects_and_performs_no_odata(self):
        self.client.force_login(self.user)
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk") as profit_get, patch(
            "pool_service.finance_imports.odata_unified_sync._collect_cashflow_chunk"
        ) as cashflow_get:
            response = self.client.post(reverse("finance_onec_odata_sync_start"))
        self.assertRedirects(response, reverse("finance_onec_import_list"))
        self.assertEqual(OneCODataSyncRun.objects.count(), 1)
        profit_get.assert_not_called()
        cashflow_get.assert_not_called()

    def test_plain_html_step_runs_one_chunk_and_redirects(self):
        self.client.force_login(self.user)
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        url = reverse("finance_onec_odata_sync_step", args=[run.id])
        with patch(
            "pool_service.finance_imports.odata_unified_sync.config_from_settings",
            return_value=config(),
        ), patch(
            "pool_service.finance_imports.odata_unified_sync._collect_profit_chunk",
            return_value=([], 1),
        ) as collector:
            response = self.client.post(url, {"cursor": "0"})
        self.assertRedirects(response, reverse("finance_onec_import_list"))
        collector.assert_called_once()
        run.refresh_from_db()
        self.assertEqual(run.cursor["version"], 1)
        self.assertEqual(run.status, OneCODataSyncRun.STATUS_COMPLETED)

    def test_repeated_plain_html_steps_finish_multichunk_run(self):
        self.client.force_login(self.user)
        self.active_profit(date(2024, 1, 1))
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        url = reverse("finance_onec_odata_sync_step", args=[run.id])
        with patch(
            "pool_service.finance_imports.odata_unified_sync.config_from_settings",
            return_value=config(),
        ), patch(
            "pool_service.finance_imports.odata_unified_sync._collect_profit_chunk",
            return_value=([], 1),
        ) as collector:
            first = self.client.post(url, {"cursor": "0"})
            run.refresh_from_db()
            second = self.client.post(url, {"cursor": str(run.cursor["version"])})
        self.assertRedirects(first, reverse("finance_onec_import_list"))
        self.assertRedirects(second, reverse("finance_onec_import_list"))
        self.assertEqual(collector.call_count, 2)
        run.refresh_from_db()
        self.assertEqual(run.status, OneCODataSyncRun.STATUS_COMPLETED)
        self.assertEqual(run.progress["completed_chunks"], 2)

    def test_step_rejects_missing_malformed_and_stale_cursor_without_collector(self):
        self.client.force_login(self.user)
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        url = reverse("finance_onec_odata_sync_step", args=[run.id])
        with patch("pool_service.finance_views.step_unified_sync") as step:
            self.assertEqual(self.client.get(url).status_code, 405)
            self.assertRedirects(
                self.client.post(url, {}), reverse("finance_onec_import_list")
            )
            self.assertRedirects(
                self.client.post(url, {"cursor": "broken"}),
                reverse("finance_onec_import_list"),
            )
            self.assertRedirects(
                self.client.post(url, {"cursor": "9"}),
                reverse("finance_onec_import_list"),
            )
        step.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.cursor["version"], 0)

    def test_stale_and_terminal_ajax_step_do_not_repeat_collector(self):
        self.client.force_login(self.user)
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        url = reverse("finance_onec_odata_sync_step", args=[run.id])
        with patch(
            "pool_service.finance_imports.odata_unified_sync.config_from_settings",
            return_value=config(),
        ), patch(
            "pool_service.finance_imports.odata_unified_sync._collect_profit_chunk",
            return_value=([], 1),
        ) as collector:
            first = self.client.post(
                url, {"cursor": "0"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )
            stale = self.client.post(
                url, {"cursor": "0"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )
            terminal = self.client.post(
                url, {"cursor": "1"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest"
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(terminal.status_code, 200)
        self.assertEqual(terminal.json()["status"], OneCODataSyncRun.STATUS_COMPLETED)
        collector.assert_called_once()
        run.refresh_from_db()
        self.assertEqual(run.cursor["version"], 1)

    def test_status_payload_is_filtered_to_current_permissions(self):
        self.client.force_login(self.user)
        run = OneCODataSyncRun.objects.create(
            organization=self.organization, requested_by=self.user,
            requested_report_types=[REPORT_PROFIT, REPORT_CASHFLOW],
            sync_scope={REPORT_PROFIT: {"start": "2025-01-01"}, REPORT_CASHFLOW: {"start": "2025-01-01"}},
            cursor={"version": 0}, progress={},
            result_summary={
                REPORT_PROFIT: {"drafts": [], "status": "pending"},
                REPORT_CASHFLOW: {"drafts": [], "status": "pending"},
            },
        )
        with patch(
            "pool_service.finance_views._onec_sync_report_types",
            return_value=[REPORT_CASHFLOW],
        ):
            response = self.client.get(
                reverse("finance_onec_odata_sync_status", args=[run.id])
            )
        self.assertEqual(set(response.json()["result"]), {REPORT_CASHFLOW})
        self.assertEqual(set(response.json()["scope"]), {REPORT_CASHFLOW})

    def test_active_lease_prevents_duplicate_claim_and_expired_lease_retries_cursor(self):
        run, _ = start_unified_sync(self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1))
        claimed, token = _claim_step(run.id, self.user, {REPORT_PROFIT}, 0)
        self.assertIsNotNone(token)
        busy, second = _claim_step(run.id, self.user, {REPORT_PROFIT}, 0)
        self.assertIsNone(second)
        self.assertEqual(busy.progress["step_state"], "busy")
        OneCODataSyncRun.objects.filter(pk=run.pk).update(
            lease_started_at=timezone.now() - timedelta(minutes=10)
        )
        reclaimed, replacement = _claim_step(run.id, self.user, {REPORT_PROFIT}, 0)
        self.assertIsNotNone(replacement)
        self.assertNotEqual(token, replacement)
        self.assertEqual(reclaimed.cursor["index"], 0)

    def test_late_worker_with_replaced_token_cannot_persist(self):
        run, _ = start_unified_sync(self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1))
        claimed, old_token = _claim_step(run.id, self.user, {REPORT_PROFIT}, 0)
        replacement = uuid.uuid4()
        OneCODataSyncRun.objects.filter(pk=run.pk).update(lease_token=replacement)
        with patch("pool_service.finance_imports.odata_unified_sync._claim_step", return_value=(claimed, old_token)), patch(
            "pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)
        ):
            result = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())
        result.refresh_from_db()
        self.assertEqual(result.lease_token, replacement)
        self.assertEqual(result.cursor["index"], 0)
        self.assertFalse(OneCImportBatch.objects.filter(status="previewed").exists())

    def test_timeout_keeps_cursor_and_retry_processes_same_chunk(self):
        run, _ = start_unified_sync(self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1))
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", side_effect=TimeoutError("https://secret.example/?password=x")):
            failed = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())
        self.assertEqual(failed.cursor["index"], 0)
        self.assertEqual(failed.cursor["version"], 0)
        self.assertNotIn("secret", str(failed.result_summary))
        self.assertEqual(failed.result_summary[REPORT_PROFIT]["error_code"], "profit_read_failed")
        self.assertEqual(failed.result_summary[REPORT_PROFIT]["error_stage"], "profit_read")
        self.assertRegex(failed.result_summary[REPORT_PROFIT]["correlation_id"], r"^[0-9a-f]{32}$")
        self.assertIsNone(failed.lease_token)
        self.assertFalse(OneCImportBatch.objects.filter(status="previewed").exists())
        self.assertFalse(OneCReportPeriodState.objects.exists())
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)):
            completed = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())
        self.assertEqual(completed.status, OneCODataSyncRun.STATUS_COMPLETED)
        self.assertEqual(OneCImportBatch.objects.filter(status="previewed").count(), 1)

    def _raw_profit_row(self):
        return SimpleNamespace(
            nomenclature_guid="33333333-3333-3333-3333-333333333333",
            customer_guid="44444444-4444-4444-4444-444444444444",
            responsible_guid="66666666-6666-6666-6666-666666666666",
        )

    def _failed_profit_stage(self, *, read=None, lookup=None, normalize=None):
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        with patch(
            "pool_service.finance_imports.odata_unified_sync.read_profit_rows",
            side_effect=read or None,
            return_value=None if read else ([self._raw_profit_row()], 1),
        ), patch(
            "pool_service.finance_imports.odata_unified_sync._read_reference_map",
            side_effect=lookup or None,
            return_value={} if not lookup else None,
        ), patch(
            "pool_service.finance_imports.odata_unified_sync._enrich_rows",
            side_effect=normalize or None,
            return_value=[] if not normalize else None,
        ):
            return step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())

    def test_config_failure_is_safe_and_does_not_create_a_preview(self):
        active, _ = self.active_profit()
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        with patch(
            "pool_service.finance_imports.odata_unified_sync.config_from_settings",
            side_effect=RuntimeError("password=secret https://fresh.example/?guid=abc"),
        ), self.assertLogs("pool_service.finance_imports.odata_unified_sync", level="ERROR") as logs:
            failed = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0)
        item = failed.result_summary[REPORT_PROFIT]
        self.assertEqual(item["error_stage"], "config")
        self.assertEqual(item["error_code"], "sync_config_failed")
        self.assertEqual(item["error"], "Не удалось проверить данные 1С. Продолжите проверку позже.")
        self.assertNotIn("secret", str(item))
        self.assertNotIn("fresh.example", str(item))
        self.assertNotIn("abc", str(item))
        self.assertNotIn("secret", "\n".join(logs.output))
        self.assertNotIn("fresh.example", "\n".join(logs.output))
        self.assertNotIn("Traceback", "\n".join(logs.output))
        self.assertEqual(failed.cursor["version"], 0)
        self.assertIsNone(failed.lease_token)
        self.assertFalse(OneCImportBatch.objects.filter(status="previewed").exists())
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch, active)
        self.assertEqual([path for path in Path(self.private.name).rglob("*") if path.is_file()], [])

    def test_profit_read_reference_and_normalization_failures_have_distinct_safe_stages(self):
        cases = (
            ("profit_read", "profit_read_failed", {"read": TimeoutError("https://secret.example/?token=x")}),
            ("profit_nomenclature_lookup", "profit_nomenclature_lookup_failed", {"lookup": RuntimeError("Authorization: Basic secret")}),
            ("profit_normalization", "profit_normalization_failed", {"normalize": ValueError("guid=abc")}),
        )
        for stage, error_code, kwargs in cases:
            with self.subTest(stage=stage):
                failed = self._failed_profit_stage(**kwargs)
                item = failed.result_summary[REPORT_PROFIT]
                self.assertEqual(item["status"], "retryable_error")
                self.assertEqual(item["error_stage"], stage)
                self.assertEqual(item["error_code"], error_code)
                self.assertNotRegex(str(item), r"secret|Authorization|guid=abc")
                self.assertEqual(failed.cursor["index"], 0)
                self.assertIsNone(failed.lease_token)
                self.assertFalse(OneCImportBatch.objects.exists())

    def test_invalid_profit_customer_guid_is_a_safe_guid_validation_failure(self):
        active, _ = self.active_profit()
        row = self._raw_profit_row()
        row.customer_guid = ""
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        with patch(
            "pool_service.finance_imports.odata_unified_sync.read_profit_rows",
            return_value=([row], 1),
        ), self.assertLogs("pool_service.finance_imports.odata_unified_sync", level="ERROR") as logs:
            failed = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())

        item = failed.result_summary[REPORT_PROFIT]
        self.assertEqual(item["status"], "retryable_error")
        self.assertEqual(item["error_stage"], "profit_reference_guid_validation")
        self.assertEqual(item["error_code"], "profit_reference_guid_validation_failed")
        self.assertEqual(item["error"], "Не удалось проверить данные 1С. Продолжите проверку позже.")
        self.assertRegex(item["correlation_id"], r"^[0-9a-f]{32}$")
        self.assertNotRegex(str(item), r"https?://|[?&=]|password|Authorization|customer_guid")
        self.assertNotRegex("\n".join(logs.output), r"https?://|password|Authorization")
        self.assertEqual(failed.cursor["index"], 0)
        self.assertEqual(failed.cursor["version"], 0)
        self.assertIsNone(failed.lease_token)
        self.assertFalse(OneCImportBatch.objects.filter(status="previewed").exists())
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch, active)
        self.assertEqual([path for path in Path(self.private.name).rglob("*") if path.is_file()], [])

    def test_profit_customer_and_responsible_lookup_failures_have_safe_substages(self):
        stages = (
            ("customer", "profit_customer_lookup", "profit_customer_lookup_failed"),
            ("responsible", "profit_responsible_lookup", "profit_responsible_lookup_failed"),
        )
        for failing_kind, stage, error_code in stages:
            with self.subTest(kind=failing_kind):
                run, _ = start_unified_sync(
                    self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
                )

                def read_reference_map(_config, kind, _guids, **_kwargs):
                    if kind == failing_kind:
                        raise RuntimeError("https://secret.example/?Authorization=secret&guid=abc")
                    return {}

                with patch(
                    "pool_service.finance_imports.odata_unified_sync.read_profit_rows",
                    return_value=([self._raw_profit_row()], 1),
                ), patch(
                    "pool_service.finance_imports.odata_unified_sync._read_reference_map",
                    side_effect=read_reference_map,
                ):
                    failed = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())

                item = failed.result_summary[REPORT_PROFIT]
                self.assertEqual(item["error_stage"], stage)
                self.assertEqual(item["error_code"], error_code)
                self.assertEqual(item["error"], "Не удалось проверить данные 1С. Продолжите проверку позже.")
                self.assertRegex(item["correlation_id"], r"^[0-9a-f]{32}$")
                self.assertNotRegex(str(item), r"https?://|Authorization|secret|guid=abc")
                self.assertEqual(failed.cursor["index"], 0)
                self.assertIsNone(failed.lease_token)
                self.assertFalse(OneCImportBatch.objects.exists())

    def test_cashflow_collection_failures_have_distinct_safe_stages(self):
        raw = SimpleNamespace(article_guid="77777777-7777-7777-7777-777777777777")
        cases = (
            ("cashflow_read", "cashflow_read_failed", {
                "read": RuntimeError("https://secret.example/?Authorization=secret"),
            }),
            ("cashflow_reference_lookup", "cashflow_reference_lookup_failed", {
                "lookup": ValueError("guid=abc"),
            }),
            ("cashflow_normalization", "cashflow_normalization_failed", {
                "normalize": RuntimeError("password=secret"),
            }),
        )
        for stage, error_code, kwargs in cases:
            with self.subTest(stage=stage):
                run, _ = start_unified_sync(
                    self.organization, self.user, [REPORT_CASHFLOW], today=date(2025, 5, 1)
                )
                with patch(
                    "pool_service.finance_imports.odata_unified_sync.read_cashflow_rows",
                    side_effect=kwargs.get("read"),
                    return_value=None if kwargs.get("read") else ([raw], 1),
                ), patch(
                    "pool_service.finance_imports.odata_unified_sync._read_articles",
                    side_effect=kwargs.get("lookup"),
                    return_value={} if not kwargs.get("lookup") else None,
                ), patch(
                    "pool_service.finance_imports.odata_unified_sync.normalise_cashflow_rows",
                    side_effect=kwargs.get("normalize"),
                    return_value=([], []) if not kwargs.get("normalize") else None,
                ):
                    failed = step_unified_sync(
                        run.id, self.user, [REPORT_CASHFLOW], 0, config=config()
                    )
                item = failed.result_summary[REPORT_CASHFLOW]
                self.assertEqual(item["error_stage"], stage)
                self.assertEqual(item["error_code"], error_code)
                self.assertNotRegex(str(item), r"secret|Authorization|guid=abc")
                self.assertEqual(failed.cursor["version"], 0)
                self.assertIsNone(failed.lease_token)
                self.assertFalse(OneCImportBatch.objects.exists())

    def test_retryable_error_ui_is_paused_and_keeps_resume_action(self):
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )
        with patch(
            "pool_service.finance_imports.odata_unified_sync._collect_profit_chunk",
            side_effect=TimeoutError("https://secret.example/?password=x"),
        ):
            step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())
        self.client.force_login(self.user)
        response = self.client.get(reverse("finance_onec_import_list"))
        self.assertContains(response, "Проверка приостановлена")
        self.assertContains(response, "не удалось получить данные валовой прибыли из 1С")
        self.assertContains(response, "Продолжить проверку")
        self.assertNotContains(response, "secret.example")
        self.assertNotContains(response, "Выполняется")

    def test_step_button_shows_loading_state_and_restores_resume_label(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("finance_onec_import_list"))
        self.assertContains(response, 'button.textContent = "Проверяем…"')
        self.assertContains(response, 'button.disabled = true')
        self.assertContains(response, 'button.textContent = "Продолжить проверку"')

    def test_snapshot_is_cleaned_if_commit_phase_rolls_back_then_retry_creates_one_preview(self):
        run, _ = start_unified_sync(self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1))
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)), patch(
            "pool_service.finance_imports.odata_unified_sync._clear_lease", side_effect=RuntimeError("after snapshot")
        ):
            with self.assertRaises(RuntimeError):
                step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())
        self.assertFalse(OneCImportBatch.objects.exists())
        self.assertEqual([path for path in Path(self.private.name).rglob("*") if path.is_file()], [])
        OneCODataSyncRun.objects.filter(pk=run.pk).update(
            lease_started_at=timezone.now() - timedelta(minutes=10)
        )
        with patch("pool_service.finance_imports.odata_unified_sync._collect_profit_chunk", return_value=([profit_row()], 1)):
            completed = step_unified_sync(run.id, self.user, [REPORT_PROFIT], 0, config=config())
        self.assertEqual(completed.status, OneCODataSyncRun.STATUS_COMPLETED)
        self.assertEqual(OneCImportBatch.objects.filter(status="previewed").count(), 1)
        self.assertEqual(completed.result_summary[REPORT_PROFIT]["drafts"], [str(OneCImportBatch.objects.get().id)])

    def test_historical_confirmed_b_becomes_reactivation_candidate(self):
        active, _ = self.active_profit(revenue="100.00")
        historical = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA, original_filename="b.json",
            stored_file="b.json", file_sha256="b" * 64,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=self.user,
            period_first=date(2025, 5, 1), period_last=date(2025, 5, 1),
        )
        raw = profit_row(revenue="120.00")
        OneCMonthlyProfit.objects.create(
            import_batch=historical, organization=self.organization,
            period_month=date(2025, 5, 1), source_recorder=RECORDER, source_row_number=1,
            manager_name=raw["manager_name"], customer_name=raw["customer_name"],
            document_name=raw["document_name"], nomenclature=raw["nomenclature"],
            article=raw["article"], nomenclature_type=raw["nomenclature_type"],
            quantity=raw["quantity"], revenue=raw["revenue"], cost=raw["cost"],
            gross_profit=raw["gross_profit"], cost_source="actual",
            analytical_gross_profit=raw["analytical_gross_profit"],
            profitability_percent=raw["profitability_percent"], source_data=raw["source_data"],
        )
        run = self.run_profit([raw])
        candidates = run.result_summary[REPORT_PROFIT]["reactivation_candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["candidate_batch_id"], str(historical.id))
        self.assertEqual(candidates[0]["expected_active_batch_id"], str(active.id))
        self.assertFalse(OneCImportBatch.objects.filter(status="previewed").exists())
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch, active)
        reactivate_confirmed_candidate(
            run.id, self.organization, self.user, REPORT_PROFIT,
            candidates[0]["month"], historical.id, candidates[0]["candidate_fingerprint"],
        )
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch, historical)
        activation_count = historical.period_activations.count()
        reactivate_confirmed_candidate(
            run.id, self.organization, self.user, REPORT_PROFIT,
            candidates[0]["month"], historical.id, candidates[0]["candidate_fingerprint"],
        )
        self.assertEqual(historical.period_activations.count(), activation_count)

    def test_reactivation_endpoint_is_post_only_and_permission_checked(self):
        self.client.force_login(self.user)
        run = OneCODataSyncRun.objects.create(
            organization=self.organization, requested_by=self.user,
            requested_report_types=[REPORT_PROFIT], result_summary={REPORT_PROFIT: {}},
        )
        url = reverse("finance_onec_odata_sync_reactivate", args=[run.id])
        self.assertEqual(self.client.get(url).status_code, 405)
        with patch("pool_service.finance_views.reactivate_confirmed_candidate") as activate:
            response = self.client.post(url, {
                "report_type": REPORT_PROFIT, "month": "2025-05-01",
                "batch_id": uuid.uuid4(), "fingerprint": "a" * 64,
            })
        self.assertEqual(response.status_code, 302)
        activate.assert_called_once()


    def test_permission_revoked_after_collector_discards_result_and_cursor(self):
        active, _ = self.active_profit(revenue="100.00")
        run, _ = start_unified_sync(
            self.organization, self.user, [REPORT_PROFIT], today=date(2025, 5, 1)
        )

        def collect_then_revoke(*args, **kwargs):
            OrganizationAccess.objects.filter(
                organization=self.organization, user=self.user
            ).delete()
            return [profit_row(revenue="120.00")], 1

        with patch(
            "pool_service.finance_imports.odata_unified_sync._collect_profit_chunk",
            side_effect=collect_then_revoke,
        ):
            result = step_unified_sync(
                run.id, self.user, [REPORT_PROFIT], 0, config=config()
            )
        self.assertEqual(result.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertEqual(result.cursor["index"], 0)
        self.assertEqual(result.cursor["version"], 0)
        self.assertEqual(
            result.result_summary[REPORT_PROFIT]["error_code"], "permission_revoked"
        )
        self.assertFalse(OneCImportBatch.objects.filter(status="previewed").exists())
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch, active)

    def _empty_confirmed_batch(self, *, source_type=OneCImportBatch.SOURCE_ODATA, metadata=None):
        return OneCImportBatch.objects.create(
            organization=self.organization,
            import_type=REPORT_PROFIT,
            source_type=source_type,
            original_filename="empty.json",
            stored_file="empty.json",
            file_sha256=uuid.uuid4().hex * 2,
            status=OneCImportBatch.STATUS_CONFIRMED,
            uploaded_by=self.user,
            period_first=date(2025, 5, 1),
            period_last=date(2025, 5, 1),
            metadata=metadata or {},
        )

    def test_empty_confirmed_odata_with_authoritative_scope_is_candidate(self):
        batch = self._empty_confirmed_batch(metadata={"scope_months": ["2025-05-01"]})
        fingerprint = month_fingerprint(REPORT_PROFIT, date(2025, 5, 1), [])
        self.assertEqual(
            _confirmed_candidate(self.organization, REPORT_PROFIT, date(2025, 5, 1), fingerprint),
            batch,
        )

    def test_empty_candidate_requires_valid_odata_scope(self):
        fingerprint = month_fingerprint(REPORT_PROFIT, date(2025, 5, 1), [])
        invalid_cases = [
            (OneCImportBatch.SOURCE_ODATA, {}),
            (OneCImportBatch.SOURCE_XLSX, {"scope_months": ["2025-05-01"]}),
            (OneCImportBatch.SOURCE_ODATA, {"scope_months": ["2025-06-01"]}),
            (OneCImportBatch.SOURCE_ODATA, {"scope_months": "2025-05-01"}),
        ]
        for source_type, metadata in invalid_cases:
            with self.subTest(source_type=source_type, metadata=metadata):
                batch = self._empty_confirmed_batch(source_type=source_type, metadata=metadata)
                self.assertIsNone(
                    _confirmed_candidate(
                        self.organization, REPORT_PROFIT, date(2025, 5, 1), fingerprint
                    )
                )
                batch.delete()

    def test_reactivation_rejects_newer_active_c_with_same_fingerprint(self):
        active_a, _ = self.active_profit(revenue="100.00")
        historical_b = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA, original_filename="b.json",
            stored_file="b.json", file_sha256="b" * 64,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=self.user,
            period_first=date(2025, 5, 1), period_last=date(2025, 5, 1),
        )
        raw_b = profit_row(revenue="120.00")
        OneCMonthlyProfit.objects.create(
            import_batch=historical_b, organization=self.organization,
            period_month=date(2025, 5, 1), source_recorder=RECORDER, source_row_number=1,
            manager_name=raw_b["manager_name"], customer_name=raw_b["customer_name"],
            document_name=raw_b["document_name"], nomenclature=raw_b["nomenclature"],
            article=raw_b["article"], nomenclature_type=raw_b["nomenclature_type"],
            quantity=raw_b["quantity"], revenue=raw_b["revenue"], cost=raw_b["cost"],
            gross_profit=raw_b["gross_profit"], cost_source="actual",
            analytical_gross_profit=raw_b["analytical_gross_profit"],
            profitability_percent=raw_b["profitability_percent"], source_data=raw_b["source_data"],
        )
        run = self.run_profit([raw_b])
        candidate = run.result_summary[REPORT_PROFIT]["reactivation_candidates"][0]
        self.assertEqual(candidate["expected_active_batch_id"], str(active_a.id))

        active_c = OneCImportBatch.objects.create(
            organization=self.organization, import_type=REPORT_PROFIT,
            source_type=OneCImportBatch.SOURCE_ODATA, original_filename="c.json",
            stored_file="c.json", file_sha256="c" * 64,
            status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=self.user,
            period_first=date(2025, 5, 1), period_last=date(2025, 5, 1),
        )
        # A different active batch is stale even when its canonical data still
        # has the same fingerprint as the active batch observed by the sync.
        raw_c = profit_row(revenue="100.00")
        OneCMonthlyProfit.objects.create(
            import_batch=active_c, organization=self.organization,
            period_month=date(2025, 5, 1), source_recorder=RECORDER, source_row_number=1,
            manager_name=raw_c["manager_name"], customer_name=raw_c["customer_name"],
            document_name=raw_c["document_name"], nomenclature=raw_c["nomenclature"],
            article=raw_c["article"], nomenclature_type=raw_c["nomenclature_type"],
            quantity=raw_c["quantity"], revenue=raw_c["revenue"], cost=raw_c["cost"],
            gross_profit=raw_c["gross_profit"], cost_source="actual",
            analytical_gross_profit=raw_c["analytical_gross_profit"],
            profitability_percent=raw_c["profitability_percent"], source_data=raw_c["source_data"],
        )
        state = OneCReportPeriodState.objects.get()
        state.active_batch = active_c
        state.save(update_fields=["active_batch", "updated_at"])
        OneCReportPeriodActivation.objects.create(
            period_state=state, batch=active_c, replaced_batch=active_a,
            activated_by=self.user,
        )
        b_activation_count = historical_b.period_activations.count()
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("finance_onec_odata_sync_reactivate", args=[run.id]),
            {
                "report_type": REPORT_PROFIT,
                "month": candidate["month"],
                "batch_id": candidate["candidate_batch_id"],
                "fingerprint": candidate["candidate_fingerprint"],
            },
        )
        self.assertEqual(response.status_code, 409)
        state.refresh_from_db()
        self.assertEqual(state.active_batch, active_c)
        self.assertEqual(historical_b.period_activations.count(), b_activation_count)


@skipUnless(connection.vendor == "mysql", "SQLite does not provide reliable select_for_update concurrency semantics")
@override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=1)
class UnifiedSyncMySQLConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.organization = Organization.objects.create(
            id=1, name="Аквалайн", paid_until=timezone.now() + timedelta(days=30)
        )
        self.user = User.objects.create_user("concurrent-owner")
        OrganizationAccess.objects.create(
            organization=self.organization, user=self.user, role="owner"
        )

    def test_concurrent_start_creates_only_one_unfinished_run(self):
        barrier = threading.Barrier(2)
        results = []
        errors = []

        def worker():
            close_old_connections()
            try:
                organization = Organization.objects.get(pk=self.organization.pk)
                user = User.objects.get(pk=self.user.pk)
                barrier.wait(timeout=5)
                run, created = start_unified_sync(
                    organization, user, [REPORT_PROFIT], today=date(2025, 5, 1)
                )
                results.append((run.pk, created))
            except Exception as exc:  # pragma: no cover - assertion reports worker errors
                errors.append(exc)
            finally:
                close_old_connections()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(len({run_id for run_id, _ in results}), 1)
        self.assertEqual(sum(1 for _, created in results if created), 1)
        self.assertEqual(OneCODataSyncRun.objects.count(), 1)
