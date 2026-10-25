"""Bounded deployment contention receipts, without application or provider access."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
UPDATE = ROOT / "update.sh"
SOURCE = UPDATE.read_text().split("<<'PYTHON_LOCK_DIAGNOSTIC'", 1)[1].split("\n", 1)[1].split("\nPYTHON_LOCK_DIAGNOSTIC", 1)[0]
HELPER = {"__name__": "lock_diagnostic_test"}
exec(compile(SOURCE, "<inline deployment lock diagnostic>", "exec"), HELPER)


class DeployLockDiagnosticTests(unittest.TestCase):
    def test_shell_and_inline_python_are_syntactically_valid(self):
        result = subprocess.run(["bash", "-n", str(UPDATE)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        compile(SOURCE, "<inline deployment lock diagnostic>", "exec")
        script = UPDATE.read_text()
        self.assertEqual(script.count("lock_diagnostic deployment 9 >&2"), 1)
        self.assertEqual(script.count("lock_diagnostic call_analysis 8 >&2"), 1)
        self.assertNotIn("/cmdline", SOURCE)
        self.assertNotIn("/environ", SOURCE)
        self.assertNotIn("subprocess", SOURCE)

    def test_parser_matches_exact_device_inode_and_only_held_flock_records(self):
        text = """1: FLOCK ADVISORY READ 123 08:0a:987 0 EOF
2: FLOCK ADVISORY WRITE 124 08:0a:987 0 EOF
2: -> FLOCK ADVISORY WRITE 125 08:0a:987 0 EOF
3: POSIX ADVISORY WRITE 126 08:0a:987 0 EOF
4: FLOCK ADVISORY READ 127 08:0b:987 0 EOF
5: FLOCK ADVISORY READ 128 08:0a:988 0 EOF
6: FLOCK ADVISORY READ -1 08:0a:987 0 EOF
7: FLOCK ADVISORY READ 129 invalid 0 EOF
malformed PRIVATE-PAYLOAD
"""
        self.assertEqual(HELPER["parse_holders"](text, (8, 10, 987)),
            [(123, "shared"), (124, "exclusive")])
        self.assertEqual(HELPER["parse_holders"](text, (8, 10, 1)), [])

    def test_missing_hidden_or_oversized_proc_reports_unavailable_without_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            with (proc / "lock").open("w") as opened:
                for contents in (None, "", "PRIVATE-PAYLOAD\n", "x" * (HELPER["MAX_LOCK_BYTES"] + 1)):
                    with self.subTest(contents_present=contents is not None):
                        if contents is not None:
                            (proc / "locks").write_text(contents)
                        result = HELPER["diagnose"]("deployment", opened.fileno(), proc)
                        self.assertEqual(result, {"role": "deployment", "state": "unavailable"})
                        self.assertNotIn(directory, json.dumps(result))
                        self.assertNotIn("PRIVATE-PAYLOAD", json.dumps(result))

    def test_metadata_uses_only_safe_executable_and_numeric_start_age(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            process = proc / "123"
            process.mkdir()
            # stat field 22 is starttime. The comm value is deliberately private.
            stat = "123 (PRIVATE-CUSTOMER ) NAME) S " + " ".join(["0"] * 18 + ["200"]) + "\n"
            (process / "stat").write_text(stat)
            (process / "exe").symlink_to("/usr/bin/python3.13")
            self.assertEqual(HELPER["process_metadata"](123, 10, 100, proc), ("python3.13", 8))
            (process / "exe").unlink()
            (process / "exe").symlink_to("/private/PRIVATE-CUSTOMER")
            self.assertEqual(HELPER["process_metadata"](123, 10, 100, proc), (None, 8))
            with patch.dict(HELPER, start_ticks=lambda _path: 999999):
                self.assertEqual(HELPER["process_metadata"](123, 10, 100, proc), (None, None))
            with patch.dict(HELPER, start_ticks=Mock(side_effect=[200, 201])):
                self.assertEqual(HELPER["process_metadata"](123, 10, 100, proc), (None, None))

    def test_changing_lock_snapshot_does_not_claim_a_current_holder(self):
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory) / "lock").open("w") as opened:
                with patch.dict(HELPER, bounded_text=Mock(side_effect=["first", "10 0", "second"]),
                        parse_holders=Mock(side_effect=[[(123, "shared")], []]),
                        process_metadata=lambda *_args: ("python3", 2)):
                    self.assertEqual(HELPER["diagnose"]("deployment", opened.fileno(), Path(directory)),
                        {"role": "deployment", "state": "unavailable"})

    def test_real_shared_and_exclusive_contention_still_exit_75_before_git_or_application(self):
        for role, lock_name, shared in (
                ("deployment", "service2-deploy.lock", True),
                ("deployment", "service2-deploy.lock", False),
                ("call_analysis", "service2-call-ai.lock", True)):
            with self.subTest(role=role, shared=shared), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                app = root / "application with spaces"
                (app / ".git").mkdir(parents=True)
                (root / "tmp").mkdir()
                python = root / "venv/bin/python"
                python.parent.mkdir(parents=True)
                python.symlink_to(sys.executable)
                lock_path = root / "tmp" / lock_name
                with lock_path.open("w") as lock:
                    fcntl.flock(lock, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                    result = subprocess.run(["bash", str(UPDATE), "a" * 40, str(app), "deploy"],
                        capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, 75, result.stderr)
                    rows = [line for line in result.stderr.splitlines() if line.startswith(HELPER["PREFIX"])]
                    self.assertEqual(len(rows), 1, result.stderr)
                    receipt = json.loads(rows[0][len(HELPER["PREFIX"]):])
                    self.assertEqual(receipt["role"], role)
                    self.assertIn(receipt["state"], {"observed", "unavailable"})
                    self.assertNotIn(directory, rows[0])
                    if receipt["state"] == "observed":
                        self.assertEqual(set(receipt), {"role", "state", "holders"})
                        observed = {(row["pid"], row["mode"]) for row in receipt["holders"]}
                        self.assertIn((os.getpid(), "shared" if shared else "exclusive"), observed)
                        for row in receipt["holders"]:
                            self.assertEqual(set(row), {"pid", "mode", "process_basename", "process_age_seconds"})
                self.assertEqual({path.name for path in app.iterdir()}, {".git"})
                self.assertNotIn("Fetching tested", result.stdout)
                self.assertNotIn("Applying migrations", result.stdout)


if __name__ == "__main__":
    unittest.main()
