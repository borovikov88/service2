import json
import os
from pathlib import Path
import shlex
import tempfile
import subprocess
import sys
from datetime import timedelta
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone
from pool_service.avito_scheduler import scheduler_state, panel_command


class AvitoSchedulerEvidenceTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app with space"
        self.app.mkdir()
        self.private = self.root / "tmp/service2-avito-cron"
        self.private.mkdir(parents=True, mode=0o700)
        self.file = self.private / "last-tick.json"
        settings = override_settings(BASE_DIR=self.app)
        settings.enable()
        self.addCleanup(settings.disable)
        self.now = timezone.now()

    def write(self, **changes):
        data = {"state": "finished", "exit_code": 0,
                "started_at": (self.now - timedelta(minutes=2)).isoformat(),
                "finished_at": (self.now - timedelta(minutes=1)).isoformat()}
        data.update(changes)
        self.file.write_text(json.dumps(data))
        self.file.chmod(0o600)

    def test_missing_evidence_is_unverified_and_does_not_create_file(self):
        self.assertEqual(scheduler_state()["code"], "unverified")
        self.assertFalse(self.file.exists())

    def test_only_successful_complete_worker_tick_is_ready(self):
        self.write()
        state = scheduler_state()
        self.assertTrue(state["ready"])
        self.assertIsNotNone(state["success_at"])
        for changes in ({"state": "running"}, {"exit_code": 1}, {"exit_code": 75}, {"exit_code": False},
                        {"finished_at": None}, {"finished_at": "PRIVATE SECRET"},
                        {"finished_at": (self.now + timedelta(hours=1)).isoformat()}):
            with self.subTest(changes=changes):
                self.write(**changes)
                state = scheduler_state()
                self.assertFalse(state["ready"])
                self.assertIsNone(state["success_at"])
                self.assertNotIn("PRIVATE", str(state))

    def test_old_success_and_future_clock_are_not_fresh_proof(self):
        self.write(started_at=(self.now - timedelta(hours=2)).isoformat(),
                   finished_at=(self.now - timedelta(hours=1)).isoformat())
        self.assertEqual(scheduler_state()["code"], "stale")
        self.write(started_at=(self.now + timedelta(hours=1)).isoformat())
        self.assertFalse(scheduler_state()["ready"])

    def test_unsafe_symlink_permissions_and_oversized_data_fail_closed(self):
        self.write()
        self.file.chmod(0o644)
        self.assertFalse(scheduler_state()["ready"])
        self.write()
        self.private.chmod(0o755)
        self.assertFalse(scheduler_state()["ready"])
        self.private.chmod(0o700)
        target = self.root / "target.json"
        self.file.rename(target)
        self.file.symlink_to(target)
        self.assertFalse(scheduler_state()["ready"])
        self.file.unlink()
        self.file.write_bytes(b"x" * 1025)
        self.file.chmod(0o600)
        self.assertFalse(scheduler_state()["ready"])

    def test_fifo_evidence_fails_closed_without_blocking_page_reader(self):
        os.mkfifo(self.file, 0o600)
        code = (
            "from django.conf import settings; "
            f"settings.configure(BASE_DIR={str(self.app)!r}, USE_TZ=True); "
            "from pool_service.avito_scheduler import scheduler_state; "
            "assert scheduler_state()['code'] == 'unverified'"
        )
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[2], timeout=3, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_timestamp_requires_timezone_and_never_displays_unvalidated_data(self):
        for value in (None, 123, "2026-01-01T00:00:00", "invalid"):
            self.write(started_at=value)
            self.assertEqual(scheduler_state()["code"], "unverified")

    def test_command_uses_exact_runtime_paths_and_never_reads_or_mutates_crontab(self):
        with patch("scripts.avito_monitor_cron.CronManager.read_table") as table, \
             patch("scripts.avito_monitor_cron.CronManager.private_directory") as directory:
            command = panel_command()
        table.assert_not_called()
        directory.assert_not_called()
        self.assertTrue(command.endswith(" >/dev/null 2>&1"))
        argv = shlex.split(command.removesuffix(" >/dev/null 2>&1"))
        self.assertEqual(argv[0:2], ["/usr/bin/env", "-i"])
        self.assertIn(str(self.root / "venv/bin/python"), argv)
        self.assertIn(str(self.app / "scripts/avito_monitor_cron.py"), argv)
        self.assertEqual(argv[-3:], ["tick", "--app-dir", str(self.app)])
        self.assertFalse(self.file.exists())
