import os
import stat
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from scripts.enable_onec_diagnostic_mcp import KEY, enable_flag


class OneCDiagnosticActivationTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = self.root / ".env"

    def tearDown(self):
        self.temporary.cleanup()

    def test_adds_flag_without_printing_or_touching_existing_values(self):
        self.env.write_text("SECRET_KEY=do-not-print\nSITE_URL=https://example.test\n", encoding="utf-8")
        os.chmod(self.env, 0o600)
        changed = enable_flag(self.env)
        self.assertTrue(changed)
        self.assertEqual(
            self.env.read_text(encoding="utf-8"),
            "SECRET_KEY=do-not-print\nSITE_URL=https://example.test\n"
            f"{KEY}=true\n",
        )
        self.assertEqual(stat.S_IMODE(self.env.stat().st_mode), 0o600)

    def test_replaces_single_existing_flag_idempotently(self):
        self.env.write_text(f"{KEY}=false\nOTHER=value\n", encoding="utf-8")
        self.assertTrue(enable_flag(self.env))
        self.assertFalse(enable_flag(self.env))
        self.assertEqual(self.env.read_text(encoding="utf-8"), f"{KEY}=true\nOTHER=value\n")

    def test_refuses_duplicate_definitions(self):
        self.env.write_text(f"{KEY}=false\nexport {KEY}=true\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "defined more than once"):
            enable_flag(self.env)

    def test_refuses_symlink(self):
        target = self.root / "real.env"
        target.write_text("A=1\n", encoding="utf-8")
        self.env.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "non-symlink"):
            enable_flag(self.env)
