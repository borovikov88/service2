"""Scheduling tests use synthetic identities and mocked collection only."""
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service.finance_imports.odata_daily_sync import (
    DailyConfigError, _permission_failure, check_configuration, daily_config, worker_tick,
)
from pool_service.finance_imports.odata_unified_sync import SUPPORTED_REPORT_TYPES
from pool_service.models import OneCODataSyncRun, Organization, OrganizationAccess

MODULE = "pool_service.finance_imports.odata_daily_sync"
NOW = datetime(2025, 1, 1, 0, 0, tzinfo=dt_timezone.utc)  # 07:00 Barnaul


class DailyFinanceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic", paid_until=timezone.now() + timedelta(days=30))
        self.user = User.objects.create_user("daily-owner")
        self.access = OrganizationAccess.objects.create(organization=self.org, user=self.user, role="owner")
        self.config = override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.org.pk),
            ONEC_FINANCE_SYNC_USER_ID=str(self.user.pk), ONEC_FINANCE_DAILY_ENABLED=True,
            ONEC_FINANCE_DAILY_TIME="06:00", ONEC_FINANCE_TIME_ZONE="Asia/Barnaul",
            ONEC_FINANCE_LOOKBACK_MONTHS="3",
        )
        self.config.enable()
        self.addCleanup(self.config.disable)
        # Coverage semantics independently exercised by integration tests.
        for target in [MODULE + ".auto_coverage_config",
                       "pool_service.finance_imports.odata_payroll_drafts.auto_coverage_config"]:
            mock = patch(target, return_value="synthetic-binding")
            mock.start()
            self.addCleanup(mock.stop)

    def run_record(self, **kwargs):
        defaults = dict(organization=self.org, requested_by=self.user,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY, requested_report_types=list(SUPPORTED_REPORT_TYPES),
            cursor={"version": 0, "index": 0, "queue": []})
        defaults.update(kwargs)
        return OneCODataSyncRun.objects.create(**defaults)

    def finish(self, run_id, user, allowed, version, **kwargs):
        run = OneCODataSyncRun.objects.get(pk=run_id)
        run.status = OneCODataSyncRun.STATUS_COMPLETED
        run.cursor = {**run.cursor, "version": version + 1}
        run.save()
        return run

    def test_daily_once_per_local_day_and_year_boundary_scope(self):
        with patch(MODULE + ".step_unified_sync", side_effect=self.finish):
            first = worker_tick(now=NOW)
            again = worker_tick(now=NOW + timedelta(hours=1))
        self.assertEqual(first["state"], "completed")
        self.assertEqual(again["state"], "already_attempted")
        self.assertEqual(OneCODataSyncRun.objects.count(), 1)
        run = OneCODataSyncRun.objects.get()
        self.assertEqual(run.sync_scope["_schedule_day"], "2025-01-01")
        for report in SUPPORTED_REPORT_TYPES:
            self.assertEqual(run.sync_scope[report]["start"], "2024-11-01")
            self.assertEqual(run.sync_scope[report]["end"], "2025-01-01")

    def test_before_local_due_time_does_not_create(self):
        result = worker_tick(now=NOW - timedelta(hours=2))
        self.assertEqual(result["state"], "not_due")
        self.assertFalse(OneCODataSyncRun.objects.exists())

    def test_disabled_schedule_creates_nothing(self):
        with override_settings(ONEC_FINANCE_DAILY_ENABLED=False):
            self.assertEqual(worker_tick(now=NOW)["state"], "not_due")
        self.assertFalse(OneCODataSyncRun.objects.exists())

    def test_manual_job_resumes_even_with_daily_disabled_and_no_scheduler_user(self):
        run = self.run_record()
        with override_settings(ONEC_FINANCE_DAILY_ENABLED=False, ONEC_FINANCE_SYNC_USER_ID=""), patch(
            MODULE + ".step_unified_sync", side_effect=self.finish
        ) as step:
            worker_tick(now=NOW)
        self.assertEqual(step.call_args.args[1].pk, self.user.pk)
        self.assertEqual(OneCODataSyncRun.objects.count(), 1)
        run.refresh_from_db()
        self.assertEqual(run.status, "completed")

    def test_preview_is_never_stepped_or_auto_applied(self):
        self.run_record(mode=OneCODataSyncRun.MODE_PREVIEW)
        with patch(MODULE + ".step_unified_sync") as step:
            result = worker_tick(now=NOW)
        self.assertEqual(result["state"], "busy")
        step.assert_not_called()
        self.assertEqual(OneCODataSyncRun.objects.count(), 1)

    def test_other_organization_run_is_untouched(self):
        other = Organization.objects.create(name="Other synthetic")
        foreign = self.run_record(organization=other)
        with override_settings(ONEC_FINANCE_DAILY_ENABLED=False), patch(MODULE + ".step_unified_sync") as step:
            worker_tick(now=NOW)
        step.assert_not_called()
        foreign.refresh_from_db()
        self.assertEqual(foreign.status, "pending")

    def test_failed_daily_attempt_is_not_recreated_every_tick(self):
        self.run_record(status=OneCODataSyncRun.STATUS_FAILED, sync_scope={"_schedule_day": "2025-01-01"})
        self.assertEqual(worker_tick(now=NOW)["state"], "already_attempted")
        self.assertEqual(OneCODataSyncRun.objects.count(), 1)

    def test_inactive_initiator_blocks_resume_even_with_privileged_schedule_actor(self):
        run = self.run_record()
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        with patch(MODULE + ".step_unified_sync") as step:
            result = worker_tick(now=NOW)
        step.assert_not_called()
        self.assertEqual(result["state"], "failed")
        run.refresh_from_db()
        self.assertEqual(run.progress["step_state"], "permission_revoked")

    def test_revoked_membership_blocks_resume(self):
        self.run_record()
        self.access.delete()
        with patch(MODULE + ".step_unified_sync") as step:
            self.assertEqual(worker_tick(now=NOW)["state"], "failed")
        step.assert_not_called()

    def test_permission_failure_does_not_change_completed_run(self):
        run = self.run_record(status=OneCODataSyncRun.STATUS_COMPLETED)
        self.assertEqual(_permission_failure(run.pk).status, "completed")

    def test_busy_cursor_stops_worker_without_looping(self):
        run = self.run_record()
        with patch(MODULE + ".step_unified_sync", return_value=run) as step:
            result = worker_tick(now=NOW)
        self.assertEqual(result["steps"], 1)
        step.assert_called_once()

    def test_retry_after_prevents_repeated_network_calls(self):
        self.run_record(progress={"worker_retry_cursor": 0,
                                 "worker_retry_after": (NOW + timedelta(minutes=15)).isoformat()})
        with patch(MODULE + ".step_unified_sync") as step:
            self.assertEqual(worker_tick(now=NOW)["state"], "retry_later")
        step.assert_not_called()

    def test_check_is_readonly_and_validates_source_and_actor(self):
        with patch(MODULE + ".validate_config") as validate, patch(MODULE + ".step_unified_sync") as step:
            check_configuration()
        validate.assert_called_once()
        step.assert_not_called()
        self.assertFalse(OneCODataSyncRun.objects.exists())
        self.access.delete()
        with self.assertRaises(PermissionDenied):
            check_configuration()

    def test_invalid_config_is_rejected(self):
        for key, value in [("ONEC_FINANCE_TIME_ZONE", "invalid"), ("ONEC_FINANCE_DAILY_TIME", "25:00"),
                           ("ONEC_FINANCE_LOOKBACK_MONTHS", "25"), ("ONEC_FINANCE_SYNC_USER_ID", "")]:
            with self.subTest(key=key), override_settings(**{key: value}), self.assertRaises(DailyConfigError):
                daily_config()

    def test_check_command_does_not_tick(self):
        with patch(MODULE + ".validate_config"), patch(
            "pool_service.management.commands.sync_onec_finance.worker_tick"
        ) as tick:
            out = StringIO()
            call_command("sync_onec_finance", check=True, stdout=out)
        self.assertIn("CONFIG_OK (read-only)", out.getvalue())
        tick.assert_not_called()

    def test_command_rejects_unbounded_limits(self):
        with self.assertRaises(CommandError):
            call_command("sync_onec_finance", max_seconds=301)

    def test_check_masks_transport_config_exception(self):
        from pool_service.finance_imports.odata_profit import ODataPreviewError
        with patch(MODULE + ".validate_config", side_effect=ODataPreviewError("synthetic-private-value")):
            with self.assertRaises(CommandError) as caught:
                call_command("sync_onec_finance", check=True)
        self.assertNotIn("synthetic-private-value", str(caught.exception))

    def test_step_limit_leaves_run_resumable(self):
        self.run_record()
        def advance(run_id, _user, _allowed, version, **kwargs):
            run = OneCODataSyncRun.objects.get(pk=run_id)
            run.cursor = {**run.cursor, "version": version + 1}
            run.status = OneCODataSyncRun.STATUS_RUNNING
            run.save()
            return run
        with patch(MODULE + ".step_unified_sync", side_effect=advance) as step:
            result = worker_tick(now=NOW, max_steps=2)
        self.assertEqual(result["steps"], 2)
        self.assertEqual(result["state"], "running")
        self.assertEqual(step.call_count, 2)

    def test_timeout_command_is_not_reported_as_success(self):
        from pool_service.management.commands.sync_onec_finance import WorkerDeadline
        with patch("pool_service.management.commands.sync_onec_finance.worker_tick", side_effect=WorkerDeadline):
            with self.assertRaisesMessage(CommandError, "WORKER_TIME_LIMIT"):
                call_command("sync_onec_finance")
