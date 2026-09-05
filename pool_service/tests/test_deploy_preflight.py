import subprocess
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase


class DeployPreflightIntegrationTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.host = self.root / "host"
        self.app = self.host / "app"
        self.script = Path(settings.BASE_DIR) / "update.sh"

        self._run("git", "init", "--bare", str(self.remote))
        self._run("git", "init", "-b", "main", str(self.seed))
        (self.seed / "manage.py").write_text("# test\n", encoding="utf-8")
        (self.seed / "requirements.txt").write_text("", encoding="utf-8")
        self._run("git", "-C", str(self.seed), "add", ".")
        self._run(
            "git", "-C", str(self.seed), "-c", "user.name=Test",
            "-c", "user.email=test@example.invalid", "commit", "-m", "initial",
        )
        self._run("git", "-C", str(self.seed), "remote", "add", "origin", str(self.remote))
        self._run("git", "-C", str(self.seed), "push", "origin", "main")
        self._run("git", "--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main")
        self._run("git", "clone", str(self.remote), str(self.app))
        (self.host / "venv/bin").mkdir(parents=True)
        python = self.host / "venv/bin/python"
        python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        python.chmod(0o755)
        (self.host / "tmp").mkdir()
        self.sha = self._output("git", "-C", str(self.app), "rev-parse", "HEAD").strip()

    def tearDown(self):
        self.temporary.cleanup()

    def _run(self, *command, check=True):
        return subprocess.run(command, check=check, text=True, capture_output=True)

    def _output(self, *command):
        return self._run(*command).stdout

    def _preflight(self, sha=None):
        return self._run(
            "bash", str(self.script), sha or self.sha, str(self.app), "preflight",
            check=False,
        )

    def _git_snapshot(self):
        return (
            self._output("git", "-C", str(self.app), "rev-parse", "HEAD"),
            self._output("git", "-C", str(self.app), "show-ref"),
        )

    def test_clean_preflight_does_not_move_head_refs_or_restart(self):
        before = self._git_snapshot()
        result = self._preflight()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._git_snapshot(), before)
        self.assertFalse((self.host / "tmp/restart.txt").exists())

    def test_dirty_files_are_preserved_byte_for_byte(self):
        tracked = self.app / "manage.py"
        tracked.write_bytes(b"local tracked bytes\n")
        untracked = self.app / "private-local.txt"
        untracked.write_bytes(b"local untracked bytes\x00")
        before = self._git_snapshot()
        result = self._preflight()
        self.assertEqual(result.returncode, 66)
        self.assertEqual(tracked.read_bytes(), b"local tracked bytes\n")
        self.assertEqual(untracked.read_bytes(), b"local untracked bytes\x00")
        self.assertEqual(self._git_snapshot(), before)
        self.assertFalse((self.host / "tmp/restart.txt").exists())

    def test_wrong_sha_is_blocked_without_checkout_or_restart(self):
        before = self._git_snapshot()
        result = self._preflight("f" * 40)
        self.assertEqual(result.returncode, 65)
        self.assertEqual(self._git_snapshot(), before)
        self.assertFalse((self.host / "tmp/restart.txt").exists())
