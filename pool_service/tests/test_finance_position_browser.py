from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.http import JsonResponse
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.odata_unified_sync import REPORT_PROFIT
from pool_service.models import OneCODataSyncRun, Organization, OrganizationAccess


@override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=1)
class FinancePositionBrowserTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            id=1,
            name="Synthetic",
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.user = User.objects.create_user("browser-owner", password="secret")
        OrganizationAccess.objects.create(
            organization=self.organization,
            user=self.user,
            role="owner",
        )
        self.client.force_login(self.user)

    def make_run(self, *, status=OneCODataSyncRun.STATUS_COMPLETED, progress=None):
        return OneCODataSyncRun.objects.create(
            organization=self.organization,
            requested_by=self.user,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            requested_report_types=[REPORT_PROFIT],
            status=status,
            cursor={"version": 3, "index": 0, "queue": []},
            progress=progress or {},
            result_summary={},
            sync_scope={},
            applied_at=timezone.now() if status == OneCODataSyncRun.STATUS_COMPLETED else None,
            finished_at=timezone.now() if status == OneCODataSyncRun.STATUS_COMPLETED else None,
        )

    def test_terminal_retry_calls_only_position_finalizer(self):
        run = self.make_run(progress={
            "step_state": "retryable_error",
            "finance_position_state": "retryable_error",
            "finance_position_error": "private upstream detail",
        })

        def finish_position(instance):
            stored = OneCODataSyncRun.objects.get(pk=instance.pk)
            stored.progress = {
                **stored.progress,
                "step_state": "completed",
                "finance_position_state": "completed",
                "finance_position_error": "",
            }
            stored.error_message = ""
            stored.save(update_fields=["progress", "error_message"])
            return "completed"

        with patch(
            "pool_service.finance_position_browser.finalize_finance_position_step",
            side_effect=finish_position,
        ) as finalizer, patch(
            "pool_service.finance_views.step_unified_sync"
        ) as monthly_step:
            response = self.client.post(
                reverse("finance_onec_refresh_apply_step", args=[run.id]),
                {"cursor": "3"},
            )

        self.assertEqual(response.status_code, 200)
        finalizer.assert_called_once()
        monthly_step.assert_not_called()
        payload = response.json()
        self.assertEqual(payload["progress"]["finance_position_state"], "completed")
        self.assertNotIn("private upstream detail", response.content.decode())

    def test_new_completion_runs_shared_finalizer_and_refreshes_payload(self):
        run = self.make_run(status=OneCODataSyncRun.STATUS_RUNNING, progress={"step_state": "running"})

        def complete_base(request, run_id):
            stored = OneCODataSyncRun.objects.get(pk=run_id)
            stored.status = OneCODataSyncRun.STATUS_COMPLETED
            stored.applied_at = timezone.now()
            stored.finished_at = stored.applied_at
            stored.progress = {**stored.progress, "step_state": "completed", "outcome": "no_change"}
            stored.save(update_fields=["status", "applied_at", "finished_at", "progress"])
            return JsonResponse({"status": "completed"})

        def finish_position(instance):
            stored = OneCODataSyncRun.objects.get(pk=instance.pk)
            stored.progress = {**stored.progress, "finance_position_state": "completed"}
            stored.save(update_fields=["progress"])
            return "completed"

        with patch(
            "pool_service.finance_position_browser._base_step",
            side_effect=complete_base,
        ), patch(
            "pool_service.finance_position_browser.finalize_finance_position_step",
            side_effect=finish_position,
        ) as finalizer:
            response = self.client.post(
                reverse("finance_onec_refresh_apply_step", args=[run.id]),
                {"cursor": "3"},
            )

        self.assertEqual(response.status_code, 200)
        finalizer.assert_called_once()
        self.assertEqual(response.json()["progress"]["finance_position_state"], "completed")

    def test_status_masks_private_finance_position_error(self):
        run = self.make_run(progress={
            "step_state": "retryable_error",
            "finance_position_state": "retryable_error",
            "finance_position_error": "https://private.example/odata secret payload",
        })
        run.error_message = "https://private.example/odata secret payload"
        run.save(update_fields=["error_message"])

        response = self.client.get(
            reverse("finance_onec_refresh_apply_status", args=[run.id])
        )

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertNotIn("private.example", body)
        self.assertNotIn("secret payload", body)
        payload = response.json()
        self.assertEqual(payload["progress"]["finance_position_state"], "retryable_error")
        self.assertEqual(
            payload["progress"]["finance_position_error"],
            "Баланс и расчёты не обновлены; предыдущий снимок сохранён.",
        )

    def test_adapter_preserves_base_denial_without_finance_lookup(self):
        run = self.make_run(progress={"finance_position_state": "retryable_error"})
        self.client.logout()
        with patch(
            "pool_service.finance_position_browser.finalize_finance_position_step"
        ) as finalizer:
            response = self.client.post(
                reverse("finance_onec_refresh_apply_step", args=[run.id]),
                {"cursor": "3"},
            )
        self.assertIn(response.status_code, {302, 403})
        finalizer.assert_not_called()
