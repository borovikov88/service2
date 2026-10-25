"""Offline receipt and protected-runner regressions; never contacts production."""
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from contextlib import redirect_stdout

from scripts import avito_outbox_receipt as receipt

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("outbox_legacy_gate_test", ROOT / ".github/tests/test_avito_readonly_workflow.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def sample():
    return {"schema_version": 1, "source": "services_outbox_incident_readonly",
        "checked_at": "2026-10-25T01:40:00+00:00", "connection_id": 4, "account_id": "446312402",
        "incident_start": "2026-10-25T01:27:00+00:00", "incident_end": "2026-10-25T01:28:00+00:00",
        "candidate_count": 1, "message": {"local_id": 123, "created_at": "2026-10-25T01:27:03+00:00",
            "status": "pending", "attempts": 0, "has_saved_error": False, "error_code": "",
            "external_id_present": False, "accepted_at": None, "sent_by_present": True,
            "body_sha256": "a" * 64, "requested_tuple_matches": True, "post_evidence": "not_claimed"},
        "reconciliation": {"state": "absent", "error_code": "", "complete": True,
            "pages": 1, "messages_checked": 4, "provider_id_present": False},
        "scheduler": {"source": "hosting_user_crontab_cli", "state": "unavailable",
            "services_outbox_entry_present": None, "other_outbox_entry_present": None,
            "account_monitor_entry_present": None, "panel_jobs_verified": False},
        "worker": {"worker_state": "not_configured", "last_started_at": None, "last_success_at": None,
            "last_error_code": "", "pending_count": 1, "oldest_pending_at": "2026-10-25T01:27:03+00:00",
            "oldest_pending_age_seconds": 60, "unknown_count": 0, "failed_count": 0, "legacy_held_count": 1},
        "webhook_chain": {"source": "avito_webhook_chain_v1", "state": "awaiting_first", "generation_bound": True,
            "authenticated_event": False, "conversation_bound": False, "message_persisted": False,
            "event_result": None, "received_at": None, "owner_notification_state": "awaiting_first",
            "owner_notification_recorded": False, "owner_notification_error": None, "push_delivery_confirmed": False},
        "customer_post_performed": False, "queue_rows_changed": False}


class ProjectionTests(unittest.TestCase):
    def test_private_provider_payload_identity_and_cron_text_are_dropped(self):
        value = sample()
        value["PRIVATE"] = "PRIVATE"
        value["message"]["body"] = "PRIVATE"
        value["message"]["conversation_id"] = "PRIVATE"
        value["reconciliation"]["provider_id"] = "PRIVATE"
        value["scheduler"]["raw_table"] = "PRIVATE"
        self.assertNotIn("PRIVATE", json.dumps(receipt.normalize(value)))

    def test_scope_unknown_absence_and_local_claim_consistency_fail_closed(self):
        for mutate in (
            lambda v: v.update(connection_id=1),
            lambda v: v.update(account_id="111"),
            lambda v: v.update(customer_post_performed=True),
            lambda v: v["reconciliation"].update(complete=False),
            lambda v: v["reconciliation"].update(pages=0),
            lambda v: v["message"].update(attempts=1),
            lambda v: v["message"].update(error_code="PRIVATE"),
            lambda v: v["scheduler"].update(panel_jobs_verified=True),
        ):
            value = sample()
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                receipt.normalize(value)

    def test_invalid_or_duplicate_frame_is_safe_failure(self):
        import tempfile
        for data in ("PRIVATE", receipt.FRAME + json.dumps(sample()) + "\n" + receipt.FRAME + json.dumps(sample())):
            with tempfile.NamedTemporaryFile() as file:
                file.write(data.encode())
                file.flush()
                output = io.StringIO()
                with redirect_stdout(output):
                    result = receipt.main([file.name])
                self.assertEqual(result, 2)
                self.assertEqual(output.getvalue(), "AVITO_OUTBOX_READONLY_UNAVAILABLE receipt_invalid\n")


class RemoteTests(unittest.TestCase):
    def setUp(self):
        legacy.RemoteTests.setUp(self)
        python = self.base / "venv/bin/python"
        python.write_text(python.read_text().replace(
            "manage.py audit_avito_readonly --connection-id 4 --account-id 446312402 --revision 15",
            "manage.py audit_avito_outbox --connection-id 4 --account-id 446312402"))

    def run_remote(self, **updates):
        return subprocess.run(["bash", str(ROOT / ".github/scripts/avito_outbox_readonly_remote.sh"), legacy.SHA, str(self.app)],
            env=self.env | updates, capture_output=True, text=True, timeout=10)

    def test_fixed_scope_only_get_audit_and_protected_post_deploy_workflow(self):
        result = self.run_remote()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        source = (ROOT / ".github/scripts/avito_outbox_readonly_remote.sh").read_text()
        self.assertIn("audit_avito_outbox --connection-id 4 --account-id 446312402", source)
        self.assertNotIn("send_avito_outbox", source)
        self.assertNotIn("audit_avito_readonly --", source)
        workflow = (ROOT / ".github/workflows/avito-outbox-readonly.yml").read_text()
        self.assertIn("environment: production", workflow)
        self.assertIn("avito_readonly_gate.py", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("schedule:", workflow)
        self.assertIn("scripts/avito_outbox_receipt.py", workflow)

    def test_stale_dirty_foreign_checkout_and_exclusive_deploy_block(self):
        for values in ({"FAKE_HEAD": "b" * 40}, {"FAKE_REMOTE": "b" * 40},
                       {"FAKE_DIRTY": "1"}, {"FAKE_ORIGIN": "https://attacker.invalid/repo.git"}):
            with self.subTest(values=values):
                self.assertNotEqual(self.run_remote(**values).returncode, 0)
                self.assertFalse(self.marker.exists())
        lock = self.base / "tmp/service2-deploy.lock"
        with lock.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertNotEqual(self.run_remote().returncode, 0)
            self.assertFalse(self.marker.exists())

    def test_shared_worker_lock_allows_read_only_diagnostics(self):
        lock = self.base / "tmp/service2-deploy.lock"
        with lock.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            result = self.run_remote()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())

    def test_symlink_and_unsafe_mutable_directory_block(self):
        outside = self.base / "outside"
        outside.touch()
        lock = self.base / "tmp/service2-deploy.lock"
        lock.symlink_to(outside)
        self.assertNotEqual(self.run_remote().returncode, 0)
        self.assertFalse(self.marker.exists())
        lock.unlink()
        (self.base / "tmp").chmod(0o770)
        self.assertNotEqual(self.run_remote().returncode, 0)
        self.assertFalse(self.marker.exists())


class RecoveryRemoteTests(unittest.TestCase):
    def setUp(self):
        legacy.RemoteTests.setUp(self)
        python = self.base / "venv/bin/python"
        python.write_text(python.read_text().replace(
            "manage.py audit_avito_readonly --connection-id 4 --account-id 446312402 --revision 15",
            "manage.py recover_avito_original_reply --expected-sha " + legacy.SHA))
        git = self.base / "bin/git"
        git.write_text(git.read_text().replace("  diff) exit", '  show) echo "$FAKE_MESSAGE" ;;' + chr(10) + '  diff) exit'))
        self.env["FAKE_MESSAGE"] = "approved [avito-outbox-original-20261025]"

    def run_remote(self, **updates):
        return subprocess.run(["bash", str(ROOT / ".github/scripts/avito_outbox_recovery_remote.sh"), legacy.SHA, str(self.app)],
            env=self.env | updates, capture_output=True, text=True, timeout=10)

    def test_only_actual_marked_release_reaches_fixed_original_operation(self):
        result = self.run_remote()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())

    def test_missing_actual_marker_blocks_before_command(self):
        result = self.run_remote(FAKE_MESSAGE="ordinary release")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
