"""Private crontab management, real child processes, and fail-closed cutover."""
from contextlib import redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("avito_cron", ROOT / "scripts/avito_monitor_cron.py")
cron = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cron)


class AvitoHostCronTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "app with 'quotes; and $dollars"
        (self.app / "scripts").mkdir(parents=True)
        (self.root / "tmp").mkdir()
        python = self.root / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        self.wrapper = self.app / "scripts/run_avito_status_monitor.sh"
        self.wrapper.write_text('''#!/bin/bash
if [[ "${1:-}" == "--status" ]]; then
    echo 'AVITO_STATUS_MONITOR_READY configured=0 enabled=0 baselined=0 due=0 running=0 failed=0'
    exit 0
fi
echo 'SECRET_SHOULD_NEVER_BE_CAPTURED'
echo '/private/identity/provider-data' >&2
exit 7
''')
        workflow = self.app / ".github/workflows/avito-status-monitor.yml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("on:\n  workflow_dispatch:\n")
        self.table = self.root / "crontab"
        self.foreign = b"# Existing jobs\nMAILTO=existing@example.invalid\n3 1 * * * existing-command --credential SECRET\n"
        self.table.write_bytes(self.foreign)
        self.mode = self.root / "mode"
        self.mode.write_text("")
        executable = self.root / "fake-crontab"
        executable.write_text(f'''#!{sys.executable}
from pathlib import Path
import sys
table = Path({str(self.table)!r})
mode = Path({str(self.mode)!r}).read_text()
if sys.argv[1] == "-l":
    if mode == "deny_read":
        print("denied /private/username SECRET", file=sys.stderr)
        raise SystemExit(1)
    if not table.exists():
        print("no crontab for " + {pwd.getpwuid(os.getuid()).pw_name!r}, file=sys.stderr)
        raise SystemExit(1)
    sys.stdout.buffer.write(table.read_bytes())
elif sys.argv[1] == "-":
    if mode == "deny_write":
        print("denied SECRET", file=sys.stderr)
        raise SystemExit(1)
    data = sys.stdin.buffer.read()
    if mode == "race_after_write":
        data += b"# concurrent external change\\n"
    table.write_bytes(data)
else:
    raise SystemExit(2)
''')
        executable.chmod(0o700)
        self.manager = cron.CronManager(self.app, "a" * 40)
        self.manager.crontab = str(executable)
        self.manager.home = self.root
        self.manager.env["HOME"] = str(self.root)
        self.release = patch.object(self.manager, "verify_release")
        self.release.start()
        self.addCleanup(self.release.stop)

    def test_preflight_is_read_only_and_preserves_foreign_table_privately(self):
        before = self.table.read_bytes()
        self.assertEqual(self.manager.preflight(), before)
        self.assertEqual(self.table.read_bytes(), before)
        self.assertFalse(self.manager.private.exists())

    def test_install_is_idempotent_preserves_all_foreign_bytes_and_private_backup(self):
        self.assertEqual(self.manager.change(), "installed")
        installed = self.table.read_bytes()
        self.assertEqual(installed, self.foreign + self.manager.block())
        self.assertEqual(self.manager.change(), "present")
        self.assertEqual(self.table.read_bytes(), installed)
        backups = list(self.manager.private.glob("before-*.crontab"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.foreign)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.private.stat().st_mode & 0o777, 0o700)

    def test_remove_is_narrow_idempotent_and_keeps_later_foreign_changes(self):
        self.manager.change()
        later = b"# added after installation\n5 3 * * * other-command\n"
        self.table.write_bytes(self.table.read_bytes() + later)
        self.assertEqual(self.manager.change(remove=True), "removed")
        self.assertEqual(self.table.read_bytes(), self.foreign + later)
        self.assertEqual(self.manager.change(remove=True), "absent")

    def test_confirmed_absent_crontab_is_supported(self):
        self.table.unlink()
        self.assertEqual(self.manager.preflight(), b"")
        self.assertEqual(self.manager.change(), "installed")
        self.assertEqual(self.table.read_bytes(), self.manager.block())

    def test_denied_crontab_is_not_treated_as_an_empty_table(self):
        self.mode.write_text("deny_read")
        with self.assertRaisesRegex(cron.CronError, "crontab_read_denied_or_failed"):
            self.manager.change()
        self.assertEqual(self.table.read_bytes(), self.foreign)
        self.assertFalse(self.manager.private.exists())

    def test_denied_install_preserves_existing_table_and_reports_failure(self):
        self.mode.write_text("deny_write")
        with self.assertRaisesRegex(cron.CronError, "crontab_install_denied_or_failed"):
            self.manager.change()
        self.assertEqual(self.table.read_bytes(), self.foreign)

    def test_missing_crontab_fails_without_fallback(self):
        self.manager.crontab = None
        with self.assertRaisesRegex(cron.CronError, "crontab_unavailable"):
            self.manager.preflight()

    def test_cron_launcher_is_required_without_requiring_new_script_before_deploy(self):
        self.assertFalse(self.manager.script.exists())
        self.manager.preflight()
        original = os.access
        with patch.object(cron.os, "access", side_effect=lambda path, mode: False if str(path) == "/usr/bin/env" else original(path, mode)):
            with self.assertRaisesRegex(cron.CronError, "cron_launcher_unavailable"):
                self.manager.preflight()

    def test_concurrent_change_before_write_aborts_without_clobbering(self):
        original = self.manager.read_table
        calls = 0
        def change_on_comparison():
            nonlocal calls
            calls += 1
            if calls == 3:
                self.table.write_bytes(self.foreign + b"# just changed\n")
            return original()
        with patch.object(self.manager, "read_table", side_effect=change_on_comparison):
            with self.assertRaisesRegex(cron.CronError, "crontab_changed_before_write"):
                self.manager.change()
        self.assertEqual(self.table.read_bytes(), self.foreign + b"# just changed\n")

    def test_readback_conflict_never_restores_stale_whole_table(self):
        self.mode.write_text("race_after_write")
        with self.assertRaisesRegex(cron.CronError, "readback_mismatch_review_required"):
            self.manager.change()
        self.assertTrue(self.table.read_bytes().endswith(b"# concurrent external change\n"))

    def test_duplicate_modified_or_untagged_monitor_entries_fail_closed(self):
        candidates = [
            self.manager.block() * 2,
            self.manager.block().replace(b"7,22,37,52", b"*"),
            cron.BEGIN + b"foreign command\n" + cron.END,
            b"* * * * * run_avito_status_monitor.sh\n",
            b"# BEGIN SERVICE2 AVITO STATUS MONITOR v1\n",
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate[:40]):
                self.table.write_bytes(self.foreign + candidate)
                with self.assertRaises(cron.CronError):
                    self.manager.preflight()

    def test_path_quoting_does_not_allow_shell_interpolation(self):
        line = self.manager.block().decode().splitlines()[1]
        parts = shlex.split(line)
        self.assertEqual(parts[:5], ["7,22,37,52", "*", "*", "*", "*"])
        self.assertIn(str(self.app), parts)
        self.assertIn(str(self.manager.script), parts)
        self.assertIn("-i", parts)
        self.assertIn("PATH=" + cron.CRON_PATH, parts)
        self.assertNotIn("MAILTO=", line)
        self.assertNotIn("CRON_TZ=", line)

    def test_invalid_paths_and_symlink_roots_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.app, target_is_directory=True)
        for value in ("relative", "/tmp/a\nb", "/tmp/percent%path", "/tmp/control\x00", str(alias), str(self.app) + "/../x"):
            with self.subTest(value=repr(value)), self.assertRaises(cron.CronError):
                cron.checked_path(value)

    def test_unexpected_exception_does_not_expose_paths_or_secrets(self):
        output = io.StringIO()
        with patch.object(cron.CronManager, "preflight", side_effect=OSError("SECRET /private/user")):
            with redirect_stderr(output):
                code = cron.main(["preflight", "--app-dir", str(self.app)])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "AVITO_CRON status=error code=operation_failed\n")

    def test_readiness_does_not_log_raw_application_failure(self):
        self.wrapper.write_text("#!/bin/bash\necho 'SECRET /private/user'\nexit 1\n")
        with self.assertRaisesRegex(cron.CronError, "cron_environment_readiness_failed"):
            self.manager.change()
        self.assertEqual(self.table.read_bytes(), self.foreign)

    def test_readiness_uses_clean_cron_environment(self):
        with patch.dict(os.environ, {"SERVICE2_PYTHON": "/untrusted/python", "UNRELATED_SECRET": "private"}):
            self.manager.readiness()
        self.assertNotIn("SERVICE2_PYTHON", self.manager.env)
        self.assertNotIn("UNRELATED_SECRET", self.manager.env)
        self.assertNotIn("SERVICE2_AVITO_CRON_SUPERVISED", self.manager.env)
        self.assertEqual(self.manager.supervised_env()["SERVICE2_AVITO_CRON_SUPERVISED"], "1")

    def test_release_mismatch_or_remaining_github_schedule_blocks_install(self):
        with patch.object(cron, "bounded", return_value=subprocess.CompletedProcess([], 0, b"b" * 40, b"")):
            with self.assertRaisesRegex(cron.CronError, "deployed_sha_mismatch"):
                cron.CronManager.verify_release(self.manager)
        (self.app / ".github/workflows/avito-status-monitor.yml").write_text("on:\n  schedule:\n  workflow_dispatch:\n")
        with patch.object(cron, "bounded", return_value=subprocess.CompletedProcess([], 0, b"a" * 40, b"")):
            with self.assertRaisesRegex(cron.CronError, "github_schedule_not_removed"):
                cron.CronManager.verify_release(self.manager)

    def test_busy_deployment_or_monitor_blocks_cron_mutation(self):
        self.manager.private_directory()
        for name in ("service2-deploy.lock", "service2-avito-status-monitor.lock"):
            with self.subTest(name=name), self.manager.lock(self.manager.tmp / name):
                with self.assertRaisesRegex(cron.CronError, "lock_busy"):
                    self.manager.change()
        self.assertEqual(self.table.read_bytes(), self.foreign)

    def test_tick_records_only_bounded_atomic_timestamps_and_exit_code(self):
        self.assertEqual(self.manager.tick(), 7)
        target = self.manager.private / "last-tick.json"
        first = target.read_bytes()
        self.assertLess(len(first), 1024)
        self.assertNotIn(b"SECRET", first)
        self.assertNotIn(str(self.app).encode(), first)
        self.assertEqual(set(json.loads(first)), {"started_at", "finished_at", "state", "exit_code"})
        self.assertEqual(json.loads(first)["exit_code"], 7)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.tick(), 7)
        self.assertFalse(list(self.manager.private.glob(".tick-*")))

    def test_overlapping_tick_preserves_current_evidence(self):
        self.manager.private_directory()
        with self.manager.lock(self.manager.private / "tick.lock"):
            self.assertEqual(self.manager.tick(), 0)
        self.assertFalse((self.manager.private / "last-tick.json").exists())

    def real_worker_fixture(self, *, exit_worker=False):
        shutil.copyfile(ROOT / "scripts/run_avito_status_monitor.sh", self.wrapper)
        command = self.app / "pool_service/management/commands/monitor_avito_statuses.py"
        command.parent.mkdir(parents=True, exist_ok=True)
        command.touch()
        (self.app / "manage.py").write_text('''
import json, os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
descendant = subprocess.Popen([
    sys.executable, "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
], close_fds=False)
Path("workers.json").write_text(json.dumps({
    "worker": os.getpid(), "descendant": descendant.pid, "group": os.getpgrp(),
}))
''' + ("raise SystemExit(0)\n" if exit_worker else "time.sleep(60)\n"))

    @staticmethod
    def process_is_running(pid):
        try:
            value = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return False
        # Killed descendants can briefly await reaping by the host's init.
        return value.rsplit(")", 1)[1].split()[0] != "Z"

    def assert_workers_stopped(self):
        workers = json.loads((self.app / "workers.json").read_text())
        pids = (workers["worker"], workers["descendant"])
        try:
            deadline = time.monotonic() + 2
            while any(self.process_is_running(pid) for pid in pids) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(any(self.process_is_running(pid) for pid in pids))
        finally:
            # A regression must fail without leaking its test subprocesses.
            if workers["group"] != os.getpgrp():
                try:
                    os.killpg(workers["group"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    @unittest.skipUnless(Path("/proc").exists(), "Linux process-state regression")
    def test_real_outer_watchdog_stops_worker_and_descendant_in_both_modes(self):
        self.real_worker_fixture()
        original = cron.bounded
        def short_watchdog(command, **kwargs):
            kwargs["timeout"] = 1
            return original(command, **kwargs)
        for mode in ("tick", "readiness"):
            with self.subTest(mode=mode):
                with patch.object(cron, "bounded", side_effect=short_watchdog):
                    if mode == "tick":
                        self.assertEqual(self.manager.tick(), 124)
                    else:
                        with self.assertRaisesRegex(cron.CronError, "process_timeout"):
                            self.manager.readiness()
                self.assert_workers_stopped()

    @unittest.skipUnless(Path("/proc").exists(), "Linux process-state regression")
    def test_supervisor_stops_descendant_when_worker_exits_before_outer_deadline(self):
        self.real_worker_fixture(exit_worker=True)
        self.assertEqual(self.manager.tick(), 0)
        self.assert_workers_stopped()

    def test_symlink_private_directory_cannot_redirect_backups(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        self.manager.private.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(cron.CronError, "unsafe_private_directory"):
            self.manager.change()
        self.assertFalse(list(elsewhere.iterdir()))


if __name__ == "__main__":
    unittest.main()
