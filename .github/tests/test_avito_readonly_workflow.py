"""Offline regressions for the protected diagnostic CI gate and SSH runner."""
import fcntl
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("avito_gate", ROOT / ".github/scripts/avito_readonly_gate.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SHA = "a" * 40

def successful_run(**updates):
    value = {"head_sha": SHA, "head_branch": "main", "event": "push", "status": "completed",
             "conclusion": "success", "path": ".github/workflows/ci-deploy.yml",
             "repository": {"full_name": gate.REPOSITORY}}
    return value | updates

class GateTests(unittest.TestCase):
    def check(self, runs, **updates):
        args = dict(event="schedule", ref="refs/heads/main", expected=SHA, manual="",
                    current={"commit": {"sha": SHA}}, runs={"workflow_runs": runs})
        return gate.validate(**(args | updates))

    def test_successful_current_main_and_manual_exact_confirmation(self):
        self.assertEqual(SHA, self.check([successful_run()]))
        self.assertEqual(SHA, self.check([successful_run()], event="workflow_dispatch", manual=SHA))

    def test_stale_head_fork_pr_and_wrong_manual_sha_cannot_reach_ssh(self):
        for updates in ({"expected": "bad"}, {"ref": "refs/heads/feature"}, {"event": "pull_request"},
                        {"current": {"commit": {"sha": "b" * 40}}},
                        {"event": "workflow_dispatch", "manual": "b" * 40}):
            with self.subTest(updates=updates), self.assertRaises(gate.GateError):
                self.check([successful_run()], **updates)
        for bad in (successful_run(event="pull_request"), successful_run(head_branch="feature"),
                    successful_run(repository={"full_name": "attacker/service2"}),
                    successful_run(path=".github/workflows/other.yml")):
            with self.assertRaises(gate.GateError):
                self.check([bad])

    def test_newer_failed_or_running_run_is_not_hidden_by_older_green(self):
        for latest in (successful_run(conclusion="failure"), successful_run(status="in_progress"),
                       successful_run(status="completed", conclusion=None)):
            with self.assertRaises(gate.GateError):
                self.check([latest, successful_run()])
        with self.assertRaises(gate.GateError):
            self.check([])

class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.app = self.base / "app"
        for path in (self.app / ".git", self.base / "tmp", self.base / "venv/bin", self.base / "bin"):
            path.mkdir(parents=True, mode=0o700)
        (self.app / "manage.py").touch()
        self.marker = self.base / "cli-called"
        self.env = os.environ | {"PATH": str(self.base / "bin") + os.pathsep + os.environ["PATH"],
                                 "FAKE_HEAD": SHA, "FAKE_REMOTE": SHA, "FAKE_DIRTY": "0",
                                 "FAKE_ORIGIN": "https://github.com/borovikov88/service2.git",
                                 "AVITO_TEST_MARKER": str(self.marker)}
        fake_git = """#!/usr/bin/env bash
set -eu
case "$1" in
  rev-parse) if [[ "$2" == HEAD ]]; then printf '%s\\n' "$FAKE_HEAD"; else pwd -P; fi ;;
  diff) exit "$FAKE_DIRTY" ;;
  config) printf '%s\\n' "$FAKE_ORIGIN" ;;
  ls-remote) printf '%s\\trefs/heads/main\\n' "$FAKE_REMOTE" ;;
  *) exit 98 ;;
esac
"""
        (self.base / "bin/git").write_text(fake_git)
        (self.base / "bin/git").chmod(0o700)
        python = "#!/usr/bin/env bash\nset -eu\nif [[ \"$1\" == -c ]]; then exec " + \
                 "'" + sys.executable + "' \"$@\"; fi\n" + \
                 '[[ "$*" == "manage.py audit_avito_readonly --connection-id 4 --account-id 446312402 --revision 15" || "$*" == "manage.py audit_avito_readonly --connection-id 4 --account-id 446312402 --current-publication" ]]\n' + \
                 'touch "$AVITO_TEST_MARKER"\n'
        (self.base / "venv/bin/python").write_text(python)
        (self.base / "venv/bin/python").chmod(0o700)

    def run_remote(self, **updates):
        return subprocess.run(["bash", str(ROOT / ".github/scripts/avito_readonly_remote.sh"), SHA, str(self.app)],
                              env=self.env | updates, capture_output=True, text=True, timeout=10)

    def test_runner_has_fixed_services_scope_and_no_legacy_goods_selection(self):
        source = (ROOT / ".github/scripts/avito_readonly_remote.sh").read_text()
        self.assertIn("--connection-id 4 --account-id 446312402 --current-publication", source)
        self.assertNotIn("--revision", source)
        self.assertNotIn("--item-ids", source)
        self.assertNotIn("7234733937", source)
        self.assertNotIn("3842842492", source)
        workflow = (ROOT / ".github/workflows/avito-readonly-audit.yml").read_text()
        self.assertIn("read-only services connection4/account446312402", workflow)
        self.assertNotIn("inputs.connection_id", workflow)

    def test_valid_current_main_invokes_only_fixed_readonly_command(self):
        result = self.run_remote()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self.marker.exists())

    def test_private_https_git_credentials_are_not_echoed(self):
        result = self.run_remote(FAKE_ORIGIN="https://PRIVATE_TOKEN@github.com/borovikov88/service2.git")
        self.assertEqual(0, result.returncode)
        self.assertNotIn("PRIVATE_TOKEN", result.stdout + result.stderr)

    def test_stale_dirty_and_foreign_origin_block_before_operator(self):
        for values in ({"FAKE_HEAD": "b" * 40}, {"FAKE_REMOTE": "b" * 40}, {"FAKE_DIRTY": "1"},
                       {"FAKE_ORIGIN": "https://attacker.invalid/borovikov88/service2.git"},
                       {"FAKE_ORIGIN": "https://attacker.invalid/path@github.com/borovikov88/service2.git"}):
            with self.subTest(values=values):
                result = self.run_remote(**values)
                self.assertNotEqual(0, result.returncode)
                self.assertFalse(self.marker.exists())

    def test_symlink_and_busy_canonical_lock_block_before_operator(self):
        lock = self.base / "tmp/service2-deploy.lock"
        outside = self.base / "outside"
        outside.touch()
        lock.symlink_to(outside)
        self.assertNotEqual(0, self.run_remote().returncode)
        self.assertFalse(self.marker.exists())
        lock.unlink()
        with lock.open("w") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertNotEqual(0, self.run_remote().returncode)
            self.assertFalse(self.marker.exists())

    def test_group_writable_mutable_directory_blocks_operator(self):
        (self.base / "tmp").chmod(0o770)
        self.assertNotEqual(0, self.run_remote().returncode)
        self.assertFalse(self.marker.exists())

if __name__ == "__main__":
    unittest.main()
