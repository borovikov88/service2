import subprocess
import sys
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
        python.symlink_to(sys.executable)
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

    def _deploy_with_new_gate(self, fail):
        import shlex
        # The existing checkout has no gate. Only the streamed new script knows
        # about it; checkout must happen before the new helper can run.
        self.assertFalse((self.app / 'scripts/crm_pre_migrate_backup.py').exists())
        scripts = self.seed / 'scripts'; scripts.mkdir()
        (scripts / 'crm_pre_migrate_backup.py').write_text(
            'from pathlib import Path\nimport sys\n'
            'for fd in (8,9):\n'
            '    assert "FLOCK  ADVISORY  WRITE" in Path(f"/proc/self/fdinfo/{fd}").read_text()\n'
            'with Path("../tmp/events").open("a") as f: f.write("backup\\n")\n'
            + ('sys.exit(2)\n' if fail else 'Path("../tmp/gate-verified").write_text(sys.argv[-1])\n'))
        (self.seed / 'manage.py').write_text(
            'from pathlib import Path\nimport sys\n'
            'with Path("../tmp/events").open("a") as f: f.write(sys.argv[1]+"\\n")\n'
            'if sys.argv[1]=="migrate":\n'
            '    assert Path("../tmp/gate-verified").exists()\n'
            '    sys.exit(42) # Stop the synthetic deployment before further operations.\n')
        self._run('git','-C',str(self.seed),'add','.')
        self._run('git','-C',str(self.seed),'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-m','new gate')
        self._run('git','-C',str(self.seed),'push','origin','main')
        target=self._output('git','-C',str(self.seed),'rev-parse','HEAD').strip()
        binary=self.host/'venv/bin/python'; binary.unlink()
        binary.write_text('#!/usr/bin/env bash\nif [[ "$1" == -m && "$2" == pip ]]; then exit 0; fi\nexec '+shlex.quote(sys.executable)+' "$@"\n')
        binary.chmod(0o755)
        (self.host/'venv/bin/activate').write_text('export PATH='+shlex.quote(str(self.host/'venv/bin'))+':"$PATH"\n')
        result=subprocess.run(['bash','-s','--',target,str(self.app)],input=self.script.read_text(),capture_output=True,text=True,timeout=20)
        self.assertEqual(self._output('git','-C',str(self.app),'rev-parse','HEAD').strip(),target)
        self.assertFalse((self.host/'tmp/restart.txt').exists())
        events=(self.host/'tmp/events').read_text().splitlines()
        return result,events

    def test_first_deploy_checkout_new_gate_before_migrate(self):
        result,events=self._deploy_with_new_gate(False)
        self.assertEqual(result.returncode,42,result.stderr)
        self.assertEqual(events,['check','backup','migrate'])

    def test_backup_failure_after_checkout_never_migrates_or_restarts(self):
        result,events=self._deploy_with_new_gate(True)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertEqual(events,['check','backup'])
