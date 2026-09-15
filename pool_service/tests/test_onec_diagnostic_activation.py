import os
import stat
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from scripts.enable_onec_diagnostic_mcp import KEY, activate_once, enable_flag


class OneCDiagnosticActivationTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = self.root / ".env"
        self.marker = self.root / "tmp" / "onec-diagnostic-mcp-activated"

    def tearDown(self):
        self.temporary.cleanup()

    def test_adds_flag_without_printing_or_touching_existing_values(self):
        self.env.write_text("SECRET_KEY=do-not-print\nSITE_URL=https://example.test\n", encoding="utf-8")
        os.chmod(self.env, 0o600)
        before = self.env.stat()
        changed = enable_flag(self.env)
        after = self.env.stat()
        self.assertTrue(changed)
        self.assertEqual(
            self.env.read_text(encoding="utf-8"),
            "SECRET_KEY=do-not-print\nSITE_URL=https://example.test\n"
            f"{KEY}=true\n",
        )
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_uid, before.st_uid)
        self.assertEqual(after.st_gid, before.st_gid)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o600)

    def test_replaces_single_existing_flag_idempotently(self):
        self.env.write_text(f"{KEY}=false\nOTHER=value\n", encoding="utf-8")
        before_inode = self.env.stat().st_ino
        self.assertTrue(enable_flag(self.env))
        self.assertFalse(enable_flag(self.env))
        self.assertEqual(self.env.stat().st_ino, before_inode)
        self.assertEqual(self.env.read_text(encoding="utf-8"), f"{KEY}=true\nOTHER=value\n")

    def test_preserves_every_untouched_byte_and_crlf(self):
        original = (
            'SECRET_KEY="left\u2028right"\r\n'.encode("utf-8")
            + f"  export {KEY} = false\r\n".encode("ascii")
            + b"BINARYISH=\xff\xfe\r\n"
        )
        self.env.write_bytes(original)
        self.assertTrue(enable_flag(self.env))
        expected = (
            'SECRET_KEY="left\u2028right"\r\n'.encode("utf-8")
            + f"{KEY}=true\r\n".encode("ascii")
            + b"BINARYISH=\xff\xfe\r\n"
        )
        self.assertEqual(self.env.read_bytes(), expected)

    def test_refuses_duplicate_definitions(self):
        self.env.write_text(f"{KEY}=false\nexport {KEY}=true\n", encoding="utf-8")
        original = self.env.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "defined more than once"):
            enable_flag(self.env)
        self.assertEqual(self.env.read_bytes(), original)

    def test_refuses_symlink(self):
        target = self.root / "real.env"
        target.write_text("A=1\n", encoding="utf-8")
        self.env.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "non-symlink"):
            enable_flag(self.env)

    def test_activation_marker_makes_later_deploys_noop(self):
        self.env.write_text(f"{KEY}=false\n", encoding="utf-8")
        self.assertEqual(activate_once(self.env, self.marker), "enabled")
        self.assertTrue(self.marker.is_file())
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        self.env.write_text(f"{KEY}=false\n", encoding="utf-8")
        self.assertEqual(activate_once(self.env, self.marker), "already_marked")
        self.assertEqual(self.env.read_text(encoding="utf-8"), f"{KEY}=false\n")

    def test_refuses_marker_symlink(self):
        self.env.write_text(f"{KEY}=false\n", encoding="utf-8")
        self.marker.parent.mkdir(parents=True)
        target = self.root / "marker-target"
        target.write_text("x\n", encoding="utf-8")
        self.marker.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
            activate_once(self.env, self.marker)
