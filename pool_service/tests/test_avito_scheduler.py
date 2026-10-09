import io
import os
from pathlib import Path
import shlex
import tempfile
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import TestCase, override_settings
from django.utils import timezone
from pool_service.avito_scheduler import HEARTBEAT_KEY, scheduler_state, panel_command
from pool_service.communication_models import AvitoSchedulerHeartbeat


class AvitoSchedulerEvidenceTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.run_id = uuid.uuid4()

    def write(self, **changes):
        data = {"run_id": self.run_id, "state": "finished", "exit_code": 0,
                "started_at": self.now - timedelta(minutes=2),
                "finished_at": self.now - timedelta(minutes=1)}
        data.update(changes)
        return AvitoSchedulerHeartbeat.objects.update_or_create(pk=HEARTBEAT_KEY, defaults=data)[0]

    def invoke(self, phase, **options):
        with patch.dict(os.environ, {"SERVICE2_AVITO_CRON_SUPERVISED": "1"}):
            call_command("avito_scheduler_heartbeat", phase, run_id=self.run_id,
                         stdout=io.StringIO(), **options)

    def test_missing_evidence_is_unverified_and_does_not_create_row(self):
        self.assertEqual(scheduler_state()["code"], "unverified")
        self.assertFalse(AvitoSchedulerHeartbeat.objects.exists())

    def test_only_successful_complete_worker_tick_is_ready(self):
        self.write()
        self.assertTrue(scheduler_state()["ready"])
        for changes in ({"state": "running"}, {"exit_code": 1}, {"exit_code": 75},
                        {"exit_code": 124}, {"exit_code": -9}, {"exit_code": None},
                        {"finished_at": None}, {"state": "unknown"},
                        {"finished_at": self.now + timedelta(hours=1)},
                        {"finished_at": self.now - timedelta(minutes=3)}):
            with self.subTest(changes=changes):
                self.write(**changes)
                state = scheduler_state()
                self.assertFalse(state["ready"])
                self.assertIsNone(state["success_at"])

    def test_old_success_and_future_clock_are_not_fresh_proof(self):
        self.write(started_at=self.now - timedelta(hours=2), finished_at=self.now - timedelta(hours=1))
        self.assertEqual(scheduler_state()["code"], "stale")
        self.write(started_at=self.now + timedelta(hours=1))
        self.assertEqual(scheduler_state()["code"], "stale")

    def test_database_failure_is_unverified_without_detail_or_write(self):
        with patch.object(AvitoSchedulerHeartbeat.objects, "filter", side_effect=OperationalError("SECRET SQL")):
            state = scheduler_state()
        self.assertEqual(state["code"], "unverified")
        self.assertNotIn("SECRET", str(state))
        self.assertFalse(AvitoSchedulerHeartbeat.objects.exists())

    def test_web_uid_and_private_files_are_not_readiness_dependencies(self):
        self.write()
        with tempfile.TemporaryDirectory() as directory, override_settings(BASE_DIR=Path(directory) / "web"):
            with patch("os.getuid", return_value=123456):
                self.assertTrue(scheduler_state()["ready"])
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_naive_timestamps_fail_closed(self):
        record = self.write()
        record.started_at = self.now.replace(tzinfo=None)
        with patch.object(AvitoSchedulerHeartbeat.objects, "filter") as query:
            query.return_value.first.return_value = record
            self.assertEqual(scheduler_state()["code"], "unverified")

    def test_begin_clears_previous_success_and_finish_records_actual_code(self):
        self.write()
        self.invoke("begin")
        self.assertEqual(scheduler_state()["code"], "running")
        record = AvitoSchedulerHeartbeat.objects.get()
        self.assertIsNone(record.finished_at)
        self.assertIsNone(record.exit_code)
        self.invoke("finish", exit_code=75)
        self.assertEqual(scheduler_state()["code"], "failed")
        self.invoke("begin")
        self.invoke("finish", exit_code=0)
        self.assertEqual(scheduler_state()["code"], "ready")
        self.assertEqual(AvitoSchedulerHeartbeat.objects.count(), 1)

    def test_missing_or_replaced_run_cannot_finish_or_refresh_success(self):
        with self.assertRaisesMessage(CommandError, "heartbeat_run_mismatch"):
            self.invoke("finish", exit_code=0)
        self.invoke("begin")
        self.run_id = uuid.uuid4()
        with self.assertRaisesMessage(CommandError, "heartbeat_run_mismatch"):
            self.invoke("finish", exit_code=0)
        self.assertEqual(scheduler_state()["code"], "running")

    def test_repeated_finish_cannot_refresh_success(self):
        self.invoke("begin")
        self.invoke("finish", exit_code=0)
        first = AvitoSchedulerHeartbeat.objects.get().finished_at
        with self.assertRaisesMessage(CommandError, "heartbeat_run_mismatch"):
            self.invoke("finish", exit_code=0)
        self.assertEqual(AvitoSchedulerHeartbeat.objects.get().finished_at, first)

    def test_helper_requires_supervisor_and_sanitizes_errors(self):
        with patch.dict(os.environ, {"SERVICE2_AVITO_CRON_SUPERVISED": "0"}):
            with self.assertRaisesMessage(CommandError, "supervisor_required"):
                call_command("avito_scheduler_heartbeat", "begin", run_id=self.run_id)
        for phase, options in (("begin", {"exit_code": 0}), ("finish", {}), ("finish", {"exit_code": 256})):
            with self.assertRaisesMessage(CommandError, "invalid_heartbeat_arguments"):
                self.invoke(phase, **options)
        with patch.object(AvitoSchedulerHeartbeat.objects, "update_or_create", side_effect=OperationalError("SECRET SQL")):
            with self.assertRaisesMessage(CommandError, "heartbeat_storage_failed") as raised:
                self.invoke("begin")
        self.assertNotIn("SECRET", str(raised.exception))
        self.assertFalse(AvitoSchedulerHeartbeat.objects.exists())

    def test_status_is_read_only_and_zero_due_worker_does_not_self_certify(self):
        for options in ({"status": True}, {}):
            call_command("monitor_avito_statuses", stdout=io.StringIO(), **options)
            self.assertFalse(AvitoSchedulerHeartbeat.objects.exists())

    def test_command_uses_exact_runtime_paths_and_never_mutates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with override_settings(BASE_DIR=root / "app with space"):
                with patch("scripts.avito_monitor_cron.CronManager.read_table") as table, \
                     patch("scripts.avito_monitor_cron.CronManager.private_directory") as private:
                    command = panel_command()
            table.assert_not_called()
            private.assert_not_called()
            argv = shlex.split(command.removesuffix(" >/dev/null 2>&1"))
            self.assertEqual(argv[0:2], ["/usr/bin/env", "-i"])
            self.assertIn(str(root / "venv/bin/python"), argv)
            self.assertEqual(argv[-3:], ["tick", "--app-dir", str(root / "app with space")])
            self.assertFalse(AvitoSchedulerHeartbeat.objects.exists())
