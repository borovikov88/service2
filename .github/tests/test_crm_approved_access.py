"""Offline receipt and real shell/lock boundary checks; no production connection."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("crm_access_receipt", ROOT / "scripts/crm_approved_access_receipt.py")
R = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(R)
SHA = "a" * 40


def receipt():
    return dict(status="applied", expected_sha=SHA, run_id=123, run_attempt=1,
                approval_id=R.APPROVAL_ID, crm_v2_read=True, crm_v2_write=True,
                requester_access=True, managers_access_verified=True,
                eligible_manager_count=3, granted_manager_count=3,
                excluded_manager_count=1, pipelines_created=4, stages_created=12)


class ReceiptTests(unittest.TestCase):
    def test_only_aggregate_bound_receipt_can_be_printed(self):
        data = receipt()
        self.assertEqual(R.normalize("private diagnostic\n" + R.PREFIX + json.dumps(data), SHA, "123", "1"), data)
        for delta in ({"email": "PRIVATE"}, {"expected_sha": "b" * 40}, {"run_id": 124},
                      {"run_attempt": True}, {"approval_id": "other"}, {"status": "dry_run"},
                      {"requester_access": False}, {"eligible_manager_count": True},
                      {"granted_manager_count": 4}, {"stages_created": -1}):
            with self.subTest(delta=delta), self.assertRaises((ValueError, TypeError)):
                R.normalize(R.PREFIX + json.dumps(data | delta), SHA, "123", "1")
        text = R.PREFIX + json.dumps(data)
        for invalid in (text + "\n" + text, text.replace('"run_id": 123', '"run_id": 123, "run_id": 123'), "x" * 16385):
            with self.assertRaises(ValueError):
                R.normalize(invalid, SHA, "123", "1")

    def test_replay_reports_revoked_access_without_claiming_or_restoring_it(self):
        data = receipt() | dict(status="already_applied", requester_access=False,
                                granted_manager_count=0, pipelines_created=0, stages_created=0)
        self.assertFalse(R.normalize(R.PREFIX + json.dumps(data), SHA, "123", "1")["requester_access"])
        with self.assertRaises(ValueError):
            R.normalize(R.PREFIX + json.dumps(data | {"granted_manager_count": 1}), SHA, "123", "1")

    def test_failure_does_not_echo_private_or_injected_diagnostics(self):
        self.assertEqual(R.failure("private\nCRM_APPROVED_ACCESS_REMOTE_BLOCKED code=deployment_busy"),
                         "CRM_APPROVED_ACCESS_BLOCKED code=deployment_busy")
        self.assertEqual(R.failure("CommandError: crm_access_membership_changed\n"
                                   "CRM_APPROVED_ACCESS_REMOTE_BLOCKED code=command_failed"),
                         "CRM_APPROVED_ACCESS_BLOCKED code=crm_access_membership_changed")
        for text in ("PRIVATE SQL", "CRM_APPROVED_ACCESS_REMOTE_BLOCKED code=PRIVATE",
                     "CRM_APPROVED_ACCESS_REMOTE_BLOCKED code=head_mismatch PRIVATE", "PRIVATE" * 4000):
            self.assertEqual(R.failure(text), "CRM_APPROVED_ACCESS_BLOCKED code=command_or_transport_failed")


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="crm-access-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.app.mkdir()
        (self.root / "tmp").mkdir()
        (self.root / "venv/bin").mkdir(parents=True)
        (self.root / "venv/bin/python").symlink_to(sys.executable)
        # A synthetic command records entry only; actual DB effects are tested by Django.
        (self.app / "manage.py").write_text("from pathlib import Path\nPath('../called').write_text('called')\n")
        self.real_git = shutil.which("git")
        self.git("init", "-q", "-b", "main")
        self.git("add", "manage.py")
        self.git("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "-qm",
                 "Approved test [crm-aqualine-managers-20261026]")
        self.git("remote", "add", "origin", "https://github.com/borovikov88/service2.git")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Intercept only remote lookup; all local Git checks and flock are real.
        wrapper = self.bin / "git"
        wrapper.write_text('#!/bin/bash\nif [[ "$1" == ls-remote ]]; then\n'
                           'printf "%s\\trefs/heads/main\\n" "$CRM_TEST_MAIN"\nelse\nexec '
                           + shlex.quote(self.real_git) + ' "$@"\nfi\n')
        wrapper.chmod(0o700)
        self.env = os.environ | {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"], "CRM_TEST_MAIN": self.sha}

    def git(self, *args):
        return subprocess.check_output([self.real_git, *args], cwd=self.app, stderr=subprocess.DEVNULL, text=True)

    def run_remote(self, sha=None):
        return subprocess.run(["bash", str(ROOT / ".github/scripts/crm_approved_access_remote.sh"),
                               sha or self.sha, str(self.app), "123", "1"],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def assert_blocked(self, code, sha=None):
        result = self.run_remote(sha)
        self.assertEqual(result.returncode, 2)
        self.assertIn("code=" + code, result.stderr)
        self.assertFalse((self.root / "called").exists())

    def test_exact_clean_current_main_reaches_fixed_command(self):
        result = self.run_remote()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "called").exists())

    def test_invalid_head_remote_main_and_dirty_code_do_not_run_command(self):
        self.assert_blocked("head_mismatch", "b" * 40)
        self.env["CRM_TEST_MAIN"] = "b" * 40
        self.assert_blocked("main_mismatch")
        self.env["CRM_TEST_MAIN"] = self.sha
        (self.app / "manage.py").write_text("PRIVATE changed code")
        self.assert_blocked("dirty_checkout")

    def test_deployment_lock_and_symlink_block_before_any_mutation(self):
        lock = self.root / "tmp/service2-deploy.lock"
        with lock.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_blocked("deployment_busy")
        lock.unlink()
        lock.symlink_to(self.app / "manage.py")
        self.assert_blocked("invalid_lock")

    def test_untracked_code_wrong_origin_or_missing_approval_cannot_run_command(self):
        (self.app / "pool_service").mkdir()
        untracked = self.app / "pool_service/injected.py"
        untracked.write_text("PRIVATE")
        self.assert_blocked("untracked_code")
        untracked.unlink()
        self.git("remote", "set-url", "origin", "https://example.invalid/private.git")
        self.assert_blocked("origin_mismatch")
        self.git("-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid", "commit", "--amend", "-qm", "Unapproved")
        self.sha = self.git("rev-parse", "HEAD").strip()
        self.assert_blocked("approval_marker_missing")


if __name__ == "__main__":
    unittest.main()
