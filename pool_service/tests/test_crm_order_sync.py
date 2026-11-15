"""CRM evidence refresh shares finance authorization and never runs from GET."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import JsonResponse
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from pool_service.finance_imports.crm_orders import sync_crm_orders
from pool_service.finance_imports.odata_daily_sync import finalize_crm_orders_step
from pool_service.finance_imports.odata_unified_sync import REPORT_PROFIT
from pool_service.models import OneCODataSyncRun, Organization, OrganizationAccess


MODULE = "pool_service.finance_imports.odata_daily_sync"


class CrmOrderSyncTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic order refresh",
            crm_v2_read=True, crm_v2_write=True, paid_until=timezone.now() + timedelta(days=30))
        self.actor = get_user_model().objects.create_user("synthetic-order-sync")
        OrganizationAccess.objects.create(user=self.actor, organization=self.org, role="owner")
        config = override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.org.pk))
        config.enable()
        self.addCleanup(config.disable)

    def make_run(self, **values):
        defaults = dict(organization=self.org, requested_by=self.actor,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY, requested_report_types=[REPORT_PROFIT],
            status=OneCODataSyncRun.STATUS_COMPLETED, progress={})
        defaults.update(values)
        return OneCODataSyncRun.objects.create(**defaults)

    def test_completed_import_refreshes_orders_once(self):
        run = self.make_run()
        with patch(MODULE + ".sync_crm_orders", return_value={"orders": 2, "completed": 1}) as sync:
            self.assertEqual(finalize_crm_orders_step(run), "completed")
            self.assertEqual(finalize_crm_orders_step(run), "completed")
        sync.assert_called_once()
        run.refresh_from_db()
        self.assertEqual(run.progress["crm_orders_count"], 2)
        self.assertEqual(run.progress["crm_orders_completed_count"], 1)

    def test_preview_running_and_disabled_crm_do_not_read_source(self):
        with patch(MODULE + ".sync_crm_orders") as sync:
            self.assertIsNone(finalize_crm_orders_step(self.make_run(mode=OneCODataSyncRun.MODE_PREVIEW)))
            self.assertIsNone(finalize_crm_orders_step(self.make_run(status=OneCODataSyncRun.STATUS_RUNNING)))
            self.org.crm_v2_read = False
            self.org.save(update_fields=["crm_v2_read"])
            self.assertEqual(finalize_crm_orders_step(self.make_run()), "disabled")
        sync.assert_not_called()

    def test_failed_read_marks_prior_evidence_stale_and_is_retryable(self):
        run = self.make_run()
        with patch(MODULE + ".sync_crm_orders", side_effect=ValidationError("private upstream text")), patch(
            "pool_service.services.crm_order_finance.mark_crm_order_sync_error"
        ) as mark:
            self.assertEqual(finalize_crm_orders_step(run), "retryable_error")
        mark.assert_called_once()
        run.refresh_from_db()
        self.assertNotIn("private", str(run.progress))
        with patch(MODULE + ".sync_crm_orders", return_value={"orders": 1, "completed": 0}):
            self.assertEqual(finalize_crm_orders_step(run), "completed")
        run.refresh_from_db()
        self.assertEqual(run.progress["crm_orders_error"], "")

    def test_revoked_actor_does_not_read_or_apply_orders(self):
        run = self.make_run()
        OrganizationAccess.objects.filter(user=self.actor).delete()
        with patch(MODULE + ".sync_crm_orders") as sync:
            self.assertEqual(finalize_crm_orders_step(run), "failed")
        sync.assert_not_called()

    def test_refresh_wrapper_rechecks_current_actor_before_network(self):
        get_user_model().objects.filter(pk=self.actor.pk).update(is_active=False)
        with patch("pool_service.finance_imports.odata_crm_orders.read_crm_orders") as read:
            with self.assertRaises(PermissionDenied):
                sync_crm_orders(self.org, self.actor)
        read.assert_not_called()

    def test_refresh_validation_uses_clock_after_slow_transport(self):
        from pool_service.finance_imports.odata_crm_orders import CrmOrdersSnapshot, source_identity
        from pool_service.finance_imports.odata_profit import ODataConfig
        started = timezone.now()
        finished = started + timedelta(minutes=3)
        config = ODataConfig(base_url="https://synthetic.example.test/odata/standard.odata/",
            username="synthetic", password="synthetic-only",
            organization_guids=("11111111-1111-4111-8111-111111111111",))
        snapshot = CrmOrdersSnapshot(started, finished, source_identity(config), (),
            {"discovery_limited": True}, full_scope=False)
        with override_settings(ONEC_ODATA_BASE_URL=config.base_url,
            ONEC_ODATA_USERNAME=config.username, ONEC_ODATA_PASSWORD=config.password,
            ONEC_ODATA_ORGANIZATION_GUIDS=config.organization_guids), patch(
            "pool_service.finance_imports.odata_crm_orders.read_crm_orders", return_value=snapshot
        ) as read, patch("pool_service.finance_imports.crm_orders.timezone.now", return_value=finished):
            result = sync_crm_orders(self.org, self.actor, now=started, config=config)
        self.assertEqual(result["orders"], 0)
        self.assertEqual(read.call_args.kwargs["now"], started)
        self.assertEqual(read.call_args.kwargs["required_order_guids"], [])

    def test_history_preserves_crm_failure_even_after_successful_payroll(self):
        from pool_service.finance_views import _finance_data_history, _finance_data_run_result
        run = self.make_run(progress={"crm_orders_state": "retryable_error", "crm_orders_error": "private details"},
            result_summary={"payroll_plan_refresh": {"created": True, "status": "completed"}})
        self.assertEqual(_finance_data_run_result(run), ("Частично выполнено", "warning"))
        request = RequestFactory().get("/synthetic-history/")
        request.user = self.actor
        history = _finance_data_history(request, self.org)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["result_tone"], "warning")
        self.assertIn("CRM не обновлены", history[0]["error_message"])
        self.assertNotIn("private", str(history))

    def test_browser_get_status_and_detail_never_finalize_orders(self):
        from pool_service import finance_position_browser as browser
        run = self.make_run()
        request = RequestFactory().get("/synthetic-read-only-status/")
        request.user = self.actor
        with patch.object(browser, "_base_status", return_value=JsonResponse({})), patch.object(
            browser, "_base_detail", return_value=JsonResponse({})
        ), patch.object(browser, "_auto_run_payload", return_value={"progress": {}}), patch.object(
            browser, "finalize_crm_orders_step"
        ) as finalize:
            self.assertEqual(browser.finance_onec_refresh_apply_status(request, run.pk).status_code, 200)
            self.assertEqual(browser.finance_onec_refresh_apply_detail(request, run.pk).status_code, 200)
        finalize.assert_not_called()

    def test_safe_browser_status_hides_upstream_diagnostics(self):
        from pool_service.finance_position_browser import _safe_payload
        run = self.make_run(progress={"crm_orders_state": "retryable_error", "crm_orders_error": "private details"})
        with patch("pool_service.finance_position_browser._auto_run_payload", return_value={"progress": {}}):
            payload = _safe_payload(run)
        self.assertNotIn("private", str(payload))
        self.assertEqual(payload["progress"]["crm_orders_state"], "retryable_error")
        self.assertEqual(payload["progress"]["step_state"], "retryable_error")
