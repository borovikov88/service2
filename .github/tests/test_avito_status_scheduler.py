"""Exercise the deployed shell wrapper with real locks and a fake application."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class AvitoStatusSchedulerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "application with spaces"
        self.app.mkdir()
        self.tmp = self.root / "tmp"
        self.tmp.mkdir()
        scripts = self.app / "scripts"
        scripts.mkdir()
        self.script = scripts / "run_avito_status_monitor.sh"
        shutil.copyfile(ROOT / "scripts/run_avito_status_monitor.sh", self.script)
        self.python = self.root / "venv/bin/python"
        self.python.parent.mkdir(parents=True)
        self.python.symlink_to(sys.executable)
        command = self.app / "pool_service/management/commands/monitor_avito_statuses.py"
        command.parent.mkdir(parents=True)
        command.touch()
        (self.app / "manage.py").write_text('''
import fcntl, json, os
from pathlib import Path
import sys
locks = {}
for name in ("service2-deploy.lock", "service2-avito-status-monitor.lock"):
    with (Path.cwd().parent / "tmp" / name).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            locks[name] = True
        else:
            locks[name] = False
            fcntl.flock(lock, fcntl.LOCK_UN)
Path("invocation.json").write_text(json.dumps({"args": sys.argv[1:], "locks": locks}))
print("AVITO_MONITOR checked=0 failures=0")
raise SystemExit(int(os.environ.get("TEST_COMMAND_EXIT", "0")))
''')
        self.env = dict(os.environ)
        self.env.pop("SERVICE2_PYTHON", None)
        self.env.pop("SERVICE2_AVITO_CRON_SUPERVISED", None)

    def invoke(self, *args):
        return subprocess.run(
            ["/bin/bash", str(self.script), *args],
            env=self.env, text=True, capture_output=True, timeout=10,
        )

    def invocation(self):
        return json.loads((self.app / "invocation.json").read_text())

    @contextmanager
    def lock(self, name, shared=False):
        with (self.tmp / name).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def tool(self, name, contents):
        folder = self.root / "test tools"
        folder.mkdir(exist_ok=True)
        script = folder / name
        script.write_text("#!/bin/sh\n" + contents)
        script.chmod(0o700)
        self.env["PATH"] = str(folder) + os.pathsep + os.environ["PATH"]
        return script

    def test_run_is_bounded_and_keeps_both_locks_until_command_finishes(self):
        record = self.root / "timeout-args"
        self.env["TIMEOUT_RECORD"] = str(record)
        self.tool("timeout", 'printf "%s\\n" "$@" > "$TIMEOUT_RECORD"\nshift 3\nexec "$@"\n')
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocation()["args"], [
            "monitor_avito_statuses", "--limit", "10", "--budget-seconds", "1500",
        ])
        self.assertEqual(self.invocation()["locks"], {
            "service2-deploy.lock": True,
            "service2-avito-status-monitor.lock": True,
        })
        self.assertEqual(record.read_text().splitlines()[:3], [
            "--signal=TERM", "--kill-after=30s", "30m",
        ])
        # No stale lock remains after the command has ended.
        with self.lock("service2-deploy.lock"), self.lock("service2-avito-status-monitor.lock"):
            pass

    def test_active_worker_skips_without_running_application(self):
        with self.lock("service2-avito-status-monitor.lock"):
            result = self.invoke()
        self.assertEqual(result.returncode, 0)
        self.assertIn("reason=worker_busy", result.stdout)
        self.assertFalse((self.app / "invocation.json").exists())

    def test_only_explicit_supervisor_uses_timeout_foreground_mode(self):
        record = self.root / "timeout-args"
        self.env["TIMEOUT_RECORD"] = str(record)
        self.tool("timeout", 'printf "%s\\n" "$@" > "$TIMEOUT_RECORD"\nexit 0\n')
        for mode in ((), ("--status",)):
            for flag, expected in ((None, False), ("0", False), ("1", True)):
                with self.subTest(mode=mode, flag=flag):
                    self.env.pop("SERVICE2_AVITO_CRON_SUPERVISED", None)
                    if flag is not None:
                        self.env["SERVICE2_AVITO_CRON_SUPERVISED"] = flag
                    self.assertEqual(self.invoke(*mode).returncode, 0)
                    self.assertEqual("--foreground" in record.read_text().splitlines(), expected)

    def test_deployment_skips_without_running_application(self):
        with self.lock("service2-deploy.lock"):
            result = self.invoke()
        self.assertEqual(result.returncode, 0)
        self.assertIn("reason=deployment_busy", result.stdout)
        self.assertFalse((self.app / "invocation.json").exists())

    def test_other_background_readers_can_share_deployment_guard(self):
        with self.lock("service2-deploy.lock", shared=True):
            result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.app / "invocation.json").exists())

    def test_status_is_read_only_and_can_run_alongside_monitor(self):
        record = self.root / "status-timeout-args"
        self.env["TIMEOUT_RECORD"] = str(record)
        self.tool("timeout", 'printf "%s\\n" "$@" > "$TIMEOUT_RECORD"\nshift 3\nexec "$@"\n')
        with self.lock("service2-avito-status-monitor.lock"):
            result = self.invoke("--status")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.invocation()["args"], ["monitor_avito_statuses", "--status"])
        self.assertTrue(self.invocation()["locks"]["service2-deploy.lock"])
        self.assertEqual(record.read_text().splitlines()[:3], [
            "--signal=TERM", "--kill-after=10s", "60s",
        ])

    def test_status_does_not_falsely_pass_readiness_during_deployment(self):
        with self.lock("service2-deploy.lock"):
            result = self.invoke("--status")
        self.assertEqual(result.returncode, 75)
        self.assertFalse((self.app / "invocation.json").exists())

    def test_scan_error_is_not_reported_as_success(self):
        self.env["TEST_COMMAND_EXIT"] = "1"
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip(), "AVITO_MONITOR status=command_failed exit_code=1")

    def test_hard_timeout_is_not_reported_as_success(self):
        self.tool("timeout", "exit 124\n")
        result = self.invoke()
        self.assertEqual(result.returncode, 124)
        self.assertIn("exit_code=124", result.stderr)
        self.assertFalse((self.app / "invocation.json").exists())

    def test_flock_failure_is_not_mistaken_for_contention(self):
        self.tool("flock", "exit 71\n")
        result = self.invoke()
        self.assertEqual(result.returncode, 71)
        self.assertIn("status=lock_failed", result.stderr)
        self.assertFalse((self.app / "invocation.json").exists())

    def test_missing_runtime_and_invalid_arguments_fail_closed(self):
        self.python.unlink()
        self.assertEqual(self.invoke().returncode, 67)
        self.assertEqual(self.invoke("--unbounded").returncode, 64)
        self.assertEqual(self.invoke("run", "extra").returncode, 64)

    def test_workflow_uses_main_production_and_existing_pinned_host_credentials(self):
        workflow = (ROOT / ".github/workflows/avito-status-monitor.yml").read_text()
        self.assertNotIn("  schedule:", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("if: github.ref == 'refs/heads/main'", workflow)
        self.assertIn("environment: production", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("timeout-minutes: 35", workflow)
        self.assertIn("StrictHostKeyChecking=yes", workflow)
        self.assertIn("exec /bin/bash scripts/run_avito_status_monitor.sh", workflow)
        self.assertNotIn("contents: write", workflow)
        self.assertNotIn("git checkout", workflow)
        self.assertNotIn("crontab", workflow)

    def test_protected_deploy_preflights_before_mutation_then_installs_exact_release(self):
        workflow = (ROOT / ".github/workflows/ci-deploy.yml").read_text()
        preflight = workflow.index("- name: Verify hosting cron capability without modifying schedules")
        deploy = workflow.index("- name: Deploy exact tested commit")
        install = workflow.index("- name: Verify cron runtime and install the single hosting schedule")
        self.assertLess(preflight, deploy)
        self.assertLess(deploy, install)
        self.assertIn('"$REMOTE_COMMAND" < scripts/avito_monitor_cron.py', workflow[preflight:deploy])
        self.assertIn("python - preflight --app-dir %q", workflow[preflight:deploy])
        self.assertIn("avito_monitor_cron.py install --app-dir %q --expected-sha %q", workflow[install:])


if __name__ == "__main__":
    unittest.main()
