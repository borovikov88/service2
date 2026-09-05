"""Real Git/SSH-script integration tests; runnable with stdlib unittest."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class GeneratedStaticDeploymentTests(unittest.TestCase):
    script = Path(__file__).resolve().parents[2] / "update.sh"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote.git"
        self.seed = self.root / "seed"
        self.host = self.root / "host"
        self.app = self.host / "app"
        self.run_command("git", "init", "--bare", str(self.remote))
        self.run_command("git", "init", "-b", "main", str(self.seed))
        self.write(self.seed, "requirements.txt", "")
        self.write(self.seed, "manage.py", '''import pathlib, shutil, sys
if sys.argv[1] == "collectstatic":
    for source in pathlib.Path("pool_service/static").rglob("*"):
        if source.is_file():
            target = pathlib.Path("public_static") / source.relative_to("pool_service/static")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
''')
        self.write(self.seed, "pool_service/static/assets/style.css", "current source\n")
        self.write(self.seed, "pool_service/static/assets/icon.png", "icon bytes\n")
        self.write(self.seed, "public_static/assets/style.css", "stale generated output\n")
        self.commit()
        self.run_command("git", "-C", str(self.seed), "remote", "add", "origin", str(self.remote))
        self.run_command("git", "-C", str(self.seed), "push", "origin", "main")
        self.run_command("git", "--git-dir", str(self.remote), "symbolic-ref", "HEAD", "refs/heads/main")
        self.run_command("git", "clone", str(self.remote), str(self.app))
        self.sha = self.git("rev-parse", "HEAD").stdout.strip()
        (self.host / "tmp").mkdir()
        python = self.host / "venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        self.write(self.host, "venv/bin/activate", '''python() {
    if [[ "$1" == "-m" && "$2" == "pip" ]]; then return 0; fi
    "''' + sys.executable + '''" "$@"
}
''')
        self.write(self.app, "public_static/assets/style.css", "current source\n")
        self.write(self.app, "public_static/assets/icon.png", "icon bytes\n")

    @staticmethod
    def write(root, relative, contents):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
        return path

    @staticmethod
    def run_command(*args, **kwargs):
        return subprocess.run(args, text=True, capture_output=True, check=True, **kwargs)

    def git(self, *args):
        return self.run_command("git", "-C", str(self.app), *args)

    def commit(self):
        self.run_command("git", "-C", str(self.seed), "add", ".")
        self.run_command("git", "-C", str(self.seed), "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")

    def advance(self, change=None):
        if change:
            change()
        else:
            self.write(self.seed, "pool_service/static/assets/style.css", "next source\n")
        self.commit()
        self.run_command("git", "-C", str(self.seed), "push", "origin", "main")
        self.sha = self.run_command("git", "-C", str(self.seed), "rev-parse", "HEAD").stdout.strip()

    def invoke(self, mode="preflight", env=None):
        return subprocess.run(["bash", "-s", "--", self.sha, str(self.app), mode], input=self.script.read_text(), text=True, capture_output=True, env=env)

    def snapshot(self):
        return {str(p.relative_to(self.host)): (p.read_bytes(), p.stat().st_mode)
                for p in self.host.rglob("*") if p.is_file() and not p.is_symlink()}

    def assert_rejected_unchanged(self):
        before = self.snapshot()
        result = self.invoke()
        self.assertEqual(result.returncode, 66, result.stdout + result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_matched_static_preflight_is_read_only_including_git_index_and_tmp(self):
        before = self.snapshot()
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_full_deploy_preserves_backup_and_repeated_collectstatic_is_accepted(self):
        self.advance()
        result = self.invoke("deploy")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout.strip(), self.sha)
        self.assertEqual((self.app / "public_static/assets/style.css").read_text(), "next source\n")
        copies = list((self.host / "tmp").glob("service2-static-*/files/public_static/assets/style.css"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), "current source\n")
        self.assertEqual(copies[0].parents[3].stat().st_mode & 0o777, 0o700)
        self.assertTrue((self.host / "tmp/restart.txt").exists())
        self.assertEqual(self.invoke().returncode, 0)
        self.assertEqual(self.invoke("deploy").returncode, 0)

    def test_real_static_edit_blocks(self):
        self.write(self.app, "public_static/assets/style.css", "local customization\n")
        self.assert_rejected_unchanged()

    def test_backups_remain_blocking_and_untouched(self):
        self.write(self.app, "backups/private.dump", "private backup\n")
        self.assert_rejected_unchanged()

    def test_matched_but_untracked_source_blocks(self):
        self.write(self.app, "pool_service/static/new.txt", "new\n")
        self.write(self.app, "public_static/new.txt", "new\n")
        self.assert_rejected_unchanged()

    def test_dirty_source_even_when_matching_output_blocks(self):
        self.write(self.app, "pool_service/static/assets/style.css", "local\n")
        self.write(self.app, "public_static/assets/style.css", "local\n")
        self.assert_rejected_unchanged()

    def test_staged_generated_change_blocks(self):
        self.git("add", "public_static/assets/style.css")
        self.assert_rejected_unchanged()

    def test_symlink_generated_file_blocks(self):
        path = self.app / "public_static/assets/style.css"
        path.unlink()
        path.symlink_to(self.app / "pool_service/static/assets/style.css")
        self.assert_rejected_unchanged()
        self.assertTrue(path.is_symlink())

    def test_unknown_generated_file_blocks(self):
        self.write(self.app, "public_static/unknown.txt", "unknown\n")
        self.assert_rejected_unchanged()

    def test_target_untracked_collision_blocks_before_static_restore(self):
        self.advance(lambda: self.write(self.seed, "public_static/assets/icon.png", "target icon\n"))
        before = (self.app / "public_static/assets/style.css").read_bytes()
        result = self.invoke("deploy")
        self.assertEqual(result.returncode, 66, result.stderr)
        self.assertEqual((self.app / "public_static/assets/style.css").read_bytes(), before)
        self.assertEqual((self.app / "public_static/assets/icon.png").read_text(), "icon bytes\n")
        self.assertFalse(list((self.host / "tmp").glob("service2-static-*")))
        self.assertFalse((self.host / "tmp/restart.txt").exists())

    def test_target_source_deletion_blocks_before_restore(self):
        self.advance(lambda: (self.seed / "pool_service/static/assets/icon.png").unlink())
        result = self.invoke("deploy")
        self.assertEqual(result.returncode, 66, result.stderr)
        self.assertEqual((self.app / "public_static/assets/style.css").read_text(), "current source\n")
        self.assertFalse(list((self.host / "tmp").glob("service2-static-*")))

    def test_target_output_parent_symlink_blocks_before_restore(self):
        def replace_parent():
            shutil.rmtree(self.seed / "public_static")
            (self.seed / "public_static").symlink_to("pool_service/static")
        self.advance(replace_parent)
        result = self.invoke("deploy")
        self.assertEqual(result.returncode, 66, result.stderr)
        self.assertFalse((self.app / "public_static").is_symlink())
        self.assertEqual((self.app / "public_static/assets/style.css").read_text(), "current source\n")
        self.assertFalse(list((self.host / "tmp").glob("service2-static-*")))

    def test_backup_directory_inside_checkout_is_rejected(self):
        self.write(self.seed, ".gitignore", "runtime-tmp/\n")
        self.advance()
        # An ignored directory would escape the dirty-file gate, so the archive
        # location needs its own containment check.
        self.git("fetch", "origin", "main")
        self.write(self.app, ".git/info/exclude", "runtime-tmp/\n")
        (self.app / "runtime-tmp").mkdir()
        (self.host / "tmp").rmdir()
        (self.host / "tmp").symlink_to(self.app / "runtime-tmp", target_is_directory=True)
        result = self.invoke("deploy")
        self.assertEqual(result.returncode, 66, result.stderr)
        self.assertIn("outside the application checkout", result.stderr)
        self.assertEqual((self.app / "public_static/assets/style.css").read_text(), "current source\n")
        self.assertFalse(list((self.app / "runtime-tmp").glob("service2-static-*")))

    def test_restore_rejects_dangling_symlink_and_keeps_backup(self):
        self.advance()
        binary_dir = self.root / "bin"
        outside = self.root / "must-not-be-created.css"
        wrapper = self.write(binary_dir, "git", '#!/bin/sh\nif [ "$1" = checkout ]; then\nrm public_static/assets/style.css\nln -s "' + str(outside) + '" public_static/assets/style.css\nexit 55\nfi\nexec "' + shutil.which("git") + '" "$@"\n')
        wrapper.chmod(0o755)
        env = dict(os.environ, PATH=str(binary_dir) + os.pathsep + os.environ["PATH"])
        result = self.invoke("deploy", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("generated output changed type", result.stderr)
        self.assertFalse(outside.exists())
        copies = list((self.host / "tmp").glob("service2-static-*/files/public_static/assets/style.css"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(), "current source\n")

    def test_failed_checkout_restores_generated_bytes(self):
        self.advance()
        binary_dir = self.root / "bin"
        wrapper = self.write(binary_dir, "git", '#!/bin/sh\nif [ "$1" = checkout ]; then exit 55; fi\nexec "' + shutil.which("git") + '" "$@"\n')
        wrapper.chmod(0o755)
        env = dict(os.environ, PATH=str(binary_dir) + os.pathsep + os.environ["PATH"])
        before = self.git("rev-parse", "HEAD").stdout
        result = self.invoke("deploy", env=env)
        self.assertEqual(result.returncode, 55, result.stdout + result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, before)
        self.assertEqual((self.app / "public_static/assets/style.css").read_text(), "current source\n")
        self.assertFalse((self.host / "tmp/restart.txt").exists())


if __name__ == "__main__":
    unittest.main()
