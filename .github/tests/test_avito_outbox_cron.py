"""Services outbox schedule scope, private supervision and truthful setup."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("outbox_cron", ROOT / "scripts/avito_outbox_cron.py")
cron = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cron)


class AvitoOutboxCronTests(unittest.TestCase):
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
        self.wrapper = self.app / "scripts/run_avito_outbox.sh"
        self.wrapper.write_text("#!/bin/bash\nexit 7\n")
        self.table = self.root / "crontab"
        self.foreign = (
            b"MAILTO=existing@example.invalid\n"
            b"7,22,37,52 * * * * existing-account-monitor --private SECRET\n"
            b"0 2 * * * unrelated-command\n")
        self.table.write_bytes(self.foreign)
        executable = self.root / "fake-crontab"
        executable.write_text(f"""#!{sys.executable}
from pathlib import Path
import sys
table = Path({str(self.table)!r})
if sys.argv[1] == "-l":
    sys.stdout.buffer.write(table.read_bytes())
elif sys.argv[1] == "-":
    table.write_bytes(sys.stdin.buffer.read())
else:
    raise SystemExit(2)
""")
        executable.chmod(0o700)
        self.manager = cron.OutboxCronManager(self.app, "a" * 40)
        self.manager.crontab = str(executable)
        self.manager.home = self.root
        self.manager.env["HOME"] = str(self.root)
        release = patch.object(self.manager, "verify_release")
        release.start()
        self.addCleanup(release.stop)
        self.release = release
        self.valid = {
            "connection_id": 4, "account_id": "446312402",
            "enabled": True, "binding_valid": True,
        }
        readiness = patch.object(self.manager, "status", return_value=self.valid)
        readiness.start()
        self.addCleanup(readiness.stop)

    def test_install_remove_are_idempotent_and_preserve_every_foreign_byte(self):
        self.assertEqual(self.manager.change(), "installed")
        installed = self.foreign + self.manager.block()
        self.assertEqual(self.table.read_bytes(), installed)
        self.assertEqual(self.manager.change(), "present")
        self.assertEqual(self.table.read_bytes(), installed)
        self.assertEqual(self.manager.change(remove=True), "removed")
        self.assertEqual(self.table.read_bytes(), self.foreign)
        self.assertEqual(self.manager.change(remove=True), "absent")

    def test_fixed_minute_panel_command_quotes_paths_and_uses_clean_environment(self):
        command = cron.panel_command(self.app)
        parts = shlex.split(command)
        self.assertIn(str(self.app), parts)
        self.assertIn(str(self.manager.script), parts)
        self.assertIn("tick", parts)
        self.assertIn("-i", parts)
        self.assertIn("TZ=UTC", parts)
        self.assertEqual(self.manager.block().decode().splitlines()[1],
                         "* * * * * " + self.manager.command())
        wrapper = (ROOT / "scripts/run_avito_outbox.sh").read_text()
        self.assertIn("send_avito_outbox --connection-id 4 --limit 10 --periodic", wrapper)
        self.assertNotIn("monitor_avito_statuses", wrapper)
        self.assertNotIn("avito_deferred_messages", wrapper)
        self.assertNotIn("SERVICE2_PYTHON", wrapper)

    def test_missing_crontab_is_not_fabricated_installation_or_a_deploy_failure(self):
        self.manager.crontab = None
        with patch.object(self.manager, "configure", return_value=self.valid), \
                patch.object(self.manager, "change") as install:
            self.assertEqual(self.manager.setup(), "panel_setup_required")
        install.assert_not_called()
        self.assertEqual(self.table.read_bytes(), self.foreign)
        self.assertFalse(self.manager.private.exists())

    def test_disabled_binding_does_not_install_or_override_owner_opt_out(self):
        disabled = {**self.valid, "enabled": False}
        with patch.object(self.manager, "configure", return_value=disabled), \
                patch.object(self.manager, "change") as install:
            self.assertEqual(self.manager.setup(), "disabled")
        install.assert_not_called()
        with patch.object(self.manager, "configure", return_value={**self.valid, "binding_valid": False}):
            with self.assertRaisesRegex(cron.CronError, "outbox_binding_invalid"):
                self.manager.setup()

    def test_unmanaged_or_modified_outbox_jobs_fail_closed_without_touching_monitor(self):
        for candidate in (
            b"* * * * * send_avito_outbox\n",
            b"* * * * * run_avito_outbox.sh\n",
            self.manager.block() * 2,
            self.manager.block().replace(b"* * * * *", b"*/5 * * * *"),
            cron.BEGIN + b"unexpected\n" + cron.END,
        ):
            with self.subTest(candidate=candidate[:50]):
                self.table.write_bytes(self.foreign + candidate)
                with self.assertRaises(cron.CronError):
                    self.manager.preflight()
                self.assertEqual(self.table.read_bytes(), self.foreign + candidate)

    def test_readiness_validates_actual_connection_binding_without_provider_calls(self):
        for value in (
            {**self.valid, "connection_id": 1},
            {**self.valid, "connection_id": True},
            {**self.valid, "account_id": "other"},
            {**self.valid, "enabled": 1},
            {**self.valid, "binding_valid": None},
            [],
        ):
            with self.subTest(value=value), self.assertRaises(cron.CronError):
                cron.OutboxCronManager.checked_status(
                    subprocess.CompletedProcess([], 0, json.dumps(value).encode(), b""))
        with self.assertRaises(cron.CronError):
            cron.OutboxCronManager.checked_status(
                subprocess.CompletedProcess([], 0, b"SECRET /private/bad-json", b""))
        self.assertEqual(cron.OutboxCronManager.checked_status(
            subprocess.CompletedProcess([], 0, json.dumps(self.valid).encode(), b"")), self.valid)

    def test_tick_records_only_private_supervision_not_successful_delivery(self):
        self.assertEqual(self.manager.tick(), 7)
        target = self.manager.private / "last-tick.json"
        state = json.loads(target.read_bytes())
        self.assertEqual(state["exit_code"], 7)
        self.assertEqual(set(state), {"started_at", "finished_at", "state", "exit_code"})
        self.assertNotIn("delivered", state)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manager.private.stat().st_mode & 0o777, 0o700)

    def test_busy_tick_or_deploy_never_overwrites_evidence_or_calls_sender(self):
        self.manager.private_directory()
        for path in (self.manager.private / "tick.lock", self.manager.tmp / "service2-deploy.lock"):
            with self.subTest(path=path.name), self.manager.lock(path), \
                    patch.object(cron, "bounded") as worker:
                self.assertEqual(self.manager.tick(), 75)
                worker.assert_not_called()
        self.assertFalse((self.manager.private / "last-tick.json").exists())

    def test_setup_has_no_sending_action_and_never_exposes_provider_stdout(self):
        with patch.object(cron, "bounded", return_value=subprocess.CompletedProcess(
                [], 0, json.dumps(self.valid).encode(), b"PRIVATE SECRET")) as worker:
            self.assertEqual(self.manager.configure(), self.valid)
        command = worker.call_args.args[0]
        self.assertEqual(command[-5:],
                         ["--connection-id", "4", "--configure", "--account-id", "446312402"])
        self.assertNotIn("--message-id", command)
        self.assertNotIn("--limit", command)

    def test_monitor_and_outbox_installers_share_one_crontab_mutation_mutex(self):
        monitor = cron._shared.CronManager(self.app, "a" * 40)
        monitor.crontab = self.manager.crontab
        for writer, blocker in ((self.manager, monitor), (monitor, self.manager)):
            with self.subTest(writer=type(writer).__name__), blocker.lock(
                    self.manager.tmp / "service2-crontab-installer.lock"):
                with self.assertRaisesRegex(cron.CronError, "lock_busy"):
                    writer.change()
        self.assertEqual(self.table.read_bytes(), self.foreign)

    def test_setup_requires_protected_exact_current_main(self):
        self.release.stop()
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
                cron.CronError, "protected_setup_required"):
            self.manager.verify_release()
        with patch.dict(os.environ, {"SERVICE2_AVITO_OUTBOX_SETUP": "1"}), \
                patch.object(cron, "bounded", return_value=subprocess.CompletedProcess(
                    [], 0, b"b" * 40, b"")), self.assertRaisesRegex(
                        cron.CronError, "deployed_main_sha_mismatch"):
            self.manager.verify_release()

    def test_workflow_runs_non_sending_setup_after_health_before_xml_setup(self):
        workflow = (ROOT / ".github/workflows/ci-deploy.yml").read_text()
        setup = workflow.index("Configure scoped Avito outgoing replies without sending the queue")
        self.assertGreater(setup, workflow.index("Verify deployed site health"))
        self.assertLess(setup, workflow.index("Set up authorised Avito services connection once"))
        self.assertIn("scripts/avito_outbox_cron.py setup", workflow)
        self.assertIn(".github/workflows/avito-outbox-readonly.yml", workflow)
        self.assertNotIn("send_avito_outbox --limit", workflow)


if __name__ == "__main__":
    unittest.main()
