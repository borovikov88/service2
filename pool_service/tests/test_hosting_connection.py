import importlib.util
import os
from pathlib import Path
import pwd
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/check_hosting_connection.py"
spec = importlib.util.spec_from_file_location("hosting_connection", SCRIPT)
hosting = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosting)


class HostingConnectionTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "DEPLOY_HOST": "hosting.example.invalid", "DEPLOY_PORT": "22",
            "DEPLOY_USER": "app", "DEPLOY_APP_PATH": "/home/app/site",
            "DEPLOY_SSH_KEY": "fake-private-key", "DEPLOY_KNOWN_HOSTS": "fake-host-key",
            "DEPLOY_HEALTH_URL": "https://example.invalid/accounts/login/",
        }

    def test_invalid_configuration_never_starts_a_process(self):
        bad = {
            "DEPLOY_HOST": ["-option", "a;touch file", "host\n", "a..b"],
            "DEPLOY_PORT": ["0", "65536", "-1", "22 -v"],
            "DEPLOY_USER": ["root;id", "-lroot", "user\n"],
            "DEPLOY_APP_PATH": ["relative", "/", "/home/app\n"],
            "DEPLOY_HEALTH_URL": ["http://x", "https://u:p@x", "https://x?",
                                  "https://x#", "https://x:65536", "https://x/ a"],
        }
        with patch.object(hosting.subprocess, "run") as run:
            for field in self.config:
                with self.subTest(missing=field), self.assertRaises(ValueError):
                    hosting.run_check({**self.config, field: ""})
            for field, values in bad.items():
                for value in values:
                    with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                        hosting.run_check({**self.config, field: value})
            run.assert_not_called()

    def _mock_run(self, command, **kwargs):
        self.commands.append((command, kwargs))
        if command[0] == "ssh-keygen":
            path = Path(command[command.index("-f") + 1])
            self.temporary_paths.append(path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        stdout = "200" if command[0] == "curl" else ""
        if command[:2] == ["ssh-keygen", "-F"]:
            stdout = "hosting.example.invalid ssh-ed25519 mocked-key\n"
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def test_success_pins_host_key_and_removes_temporary_secrets(self):
        self.commands, self.temporary_paths = [], []
        self.config["DEPLOY_PORT"] = "2222"
        self.config["DEPLOY_APP_PATH"] = "/home/app/a 'quoted' site"
        with patch.object(hosting.subprocess, "run", side_effect=self._mock_run):
            hosting.run_check(self.config)
        lookup = self.commands[1][0]
        self.assertIn("[hosting.example.invalid]:2222", lookup)
        ssh, kwargs = self.commands[3]
        for option in ("BatchMode=yes", "IdentitiesOnly=yes", "IdentityAgent=none",
                       "StrictHostKeyChecking=yes", "GlobalKnownHostsFile=/dev/null"):
            self.assertIn(option, ssh)
        self.assertEqual(kwargs["input"], hosting.REMOTE_CHECK)
        import shlex
        self.assertEqual(shlex.split(ssh[-1])[-2:], ["app", self.config["DEPLOY_APP_PATH"]])
        self.assertNotIn("--location", self.commands[4][0])
        for path in self.temporary_paths:
            self.assertFalse(path.exists())

    def test_redirect_is_not_health_success(self):
        self.commands, self.temporary_paths = [], []
        def redirect(command, **kwargs):
            result = self._mock_run(command, **kwargs)
            if command[0] == "curl":
                result.stdout = "302"
            return result
        with patch.object(hosting.subprocess, "run", side_effect=redirect):
            with self.assertRaisesRegex(ValueError, "HTTP 2xx"):
                hosting.run_check(self.config)
        self.assertTrue(all(not p.exists() for p in self.temporary_paths))

    def test_malformed_private_key_fails_before_ssh_or_curl(self):
        real_run = subprocess.run
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            return real_run(command, **kwargs)
        with patch.object(hosting.subprocess, "run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                hosting.run_check(self.config)
        self.assertEqual([c[0] for c in commands], ["ssh-keygen"])
        self.assertFalse(Path(commands[0][-1]).exists())

    def test_ssh_failure_cleans_keys_and_does_not_attempt_health_check(self):
        self.commands, self.temporary_paths = [], []
        def fail_ssh(command, **kwargs):
            result = self._mock_run(command, **kwargs)
            if command[0] == "ssh":
                raise subprocess.CalledProcessError(255, command)
            return result
        with patch.object(hosting.subprocess, "run", side_effect=fail_ssh):
            with self.assertRaises(subprocess.CalledProcessError):
                hosting.run_check(self.config)
        self.assertNotIn("curl", [c[0][0] for c in self.commands])
        self.assertTrue(all(not p.exists() for p in self.temporary_paths))

    def test_missing_or_malformed_matched_host_key_fails_before_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / "key"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                            "-f", str(private)], check=True)
            self.config["DEPLOY_SSH_KEY"] = private.read_text()
            valid = private.with_suffix(".pub").read_text()
            unrelated = "unrelated.example.invalid " + valid
            cases = [unrelated, unrelated + "hosting.example.invalid ssh-ed25519 invalid\n"]
            real_run = subprocess.run
            for hosts in cases:
                with self.subTest(hosts=hosts):
                    self.config["DEPLOY_KNOWN_HOSTS"] = hosts
                    commands = []
                    def only_keygen(command, **kwargs):
                        commands.append(command)
                        self.assertEqual(command[0], "ssh-keygen")
                        return real_run(command, **kwargs)
                    with patch.object(hosting.subprocess, "run", side_effect=only_keygen):
                        with self.assertRaises(subprocess.CalledProcessError):
                            hosting.run_check(self.config)
                    self.assertFalse(Path(commands[0][-1]).exists())

    def test_remote_check_preserves_dirty_checkout_and_all_fixture_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app"
            app.mkdir()
            (root / "venv/bin").mkdir(parents=True)
            (root / "tmp").mkdir()
            python = root / "venv/bin/python"
            python.write_text("#!/bin/sh\nprintf 'fixture interpreter version\\n'\n")
            python.chmod(0o700)
            (app / "manage.py").write_text("raise Exception('must never execute')\n")
            def git(*args):
                return subprocess.run(["git", "-C", str(app), *args], check=True,
                                      capture_output=True, text=True)
            git("init")
            git("add", "manage.py")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                "commit", "-m", "fixture")
            (app / "manage.py").write_text("local dirty bytes\n")
            (app / "untracked").write_bytes(b"preserve\x00")
            def snapshot():
                return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode)
                        for p in root.rglob("*") if p.is_file()}
            before = snapshot()
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-s", "--",
                 pwd.getpwuid(os.getuid()).pw_name, str(app)],
                input=hosting.REMOTE_CHECK, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("HOSTING_LAYOUT_OK", result.stdout)
            self.assertEqual(before, snapshot())
            self.assertFalse((root / "tmp/restart.txt").exists())
