import os
import stat
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from scripts.enable_onec_diagnostic_mcp import (
    CURRENT_MARKER,
    ENABLED_KEY,
    ISSUER_KEY,
    LEGACY_MARKER,
    PENDING_MARKER,
    PRODUCTION_ISSUER_URL,
    PRODUCTION_RESOURCE_URL,
    RESOURCE_KEY,
    activate_once,
    configure_production,
    finalize_marker,
)


class OneCDiagnosticActivationTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = self.root / ".env"
        self.marker = self.root / "tmp" / "onec-diagnostic-mcp-activated"

    def tearDown(self):
        self.temporary.cleanup()

    def expected_configuration(self):
        return (
            f"{ENABLED_KEY}=true\n"
            f"{RESOURCE_KEY}={PRODUCTION_RESOURCE_URL}\n"
            f"{ISSUER_KEY}={PRODUCTION_ISSUER_URL}\n"
        )

    def test_adds_configuration_without_touching_existing_values(self):
        self.env.write_text("SECRET_KEY=do-not-print\nSITE_URL=https://example.test\n", encoding="utf-8")
        os.chmod(self.env, 0o600)
        before = self.env.stat()
        changed = configure_production(self.env)
        after = self.env.stat()
        self.assertTrue(changed)
        self.assertEqual(
            self.env.read_text(encoding="utf-8"),
            "SECRET_KEY=do-not-print\nSITE_URL=https://example.test\n"
            + self.expected_configuration(),
        )
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_uid, before.st_uid)
        self.assertEqual(after.st_gid, before.st_gid)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o600)

    def test_replaces_existing_configuration_idempotently(self):
        self.env.write_text(
            f"{ENABLED_KEY}=false\n"
            f"{RESOURCE_KEY}=https://wrong.example/mcp/1c\n"
            f"{ISSUER_KEY}=https://wrong.example/issuer\n"
            "OTHER=value\n",
            encoding="utf-8",
        )
        before_inode = self.env.stat().st_ino
        self.assertTrue(configure_production(self.env))
        self.assertFalse(configure_production(self.env))
        self.assertEqual(self.env.stat().st_ino, before_inode)
        self.assertEqual(
            self.env.read_text(encoding="utf-8"),
            self.expected_configuration() + "OTHER=value\n",
        )

    def test_preserves_every_untouched_byte_and_crlf(self):
        original = (
            'SECRET_KEY="left\u2028right"\r\n'.encode("utf-8")
            + f"  export {ENABLED_KEY} = false\r\n".encode("ascii")
            + b"BINARYISH=\xff\xfe\r\n"
            + f"{RESOURCE_KEY}=https://wrong.example/resource\r\n".encode("ascii")
            + f"{ISSUER_KEY}=https://wrong.example/issuer\r\n".encode("ascii")
        )
        self.env.write_bytes(original)
        self.assertTrue(configure_production(self.env))
        expected = (
            'SECRET_KEY="left\u2028right"\r\n'.encode("utf-8")
            + f"{ENABLED_KEY}=true\r\n".encode("ascii")
            + b"BINARYISH=\xff\xfe\r\n"
            + f"{RESOURCE_KEY}={PRODUCTION_RESOURCE_URL}\r\n".encode("ascii")
            + f"{ISSUER_KEY}={PRODUCTION_ISSUER_URL}\r\n".encode("ascii")
        )
        self.assertEqual(self.env.read_bytes(), expected)

    def test_refuses_duplicate_definitions(self):
        self.env.write_text(
            f"{ENABLED_KEY}=false\nexport {ENABLED_KEY}=true\n",
            encoding="utf-8",
        )
        original = self.env.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "defined more than once"):
            configure_production(self.env)
        self.assertEqual(self.env.read_bytes(), original)

    def test_refuses_symlink(self):
        target = self.root / "real.env"
        target.write_text("A=1\n", encoding="utf-8")
        self.env.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "non-symlink"):
            configure_production(self.env)

    def test_legacy_marker_becomes_pending_and_repairs_metadata(self):
        self.env.write_text(
            f"{ENABLED_KEY}=true\n"
            f"{RESOURCE_KEY}=https://wrong.example/resource\n"
            f"{ISSUER_KEY}=https://wrong.example/issuer\n",
            encoding="utf-8",
        )
        self.marker.parent.mkdir(parents=True)
        self.marker.write_bytes(LEGACY_MARKER)
        self.assertEqual(activate_once(self.env, self.marker), "configured")
        self.assertEqual(self.marker.read_bytes(), PENDING_MARKER)
        self.assertEqual(stat.S_IMODE(self.marker.stat().st_mode), 0o600)
        self.assertEqual(self.env.read_text(encoding="utf-8"), self.expected_configuration())

    def test_pending_marker_forces_revalidation_until_finalized(self):
        self.env.write_text(f"{ENABLED_KEY}=false\n", encoding="utf-8")
        self.assertEqual(activate_once(self.env, self.marker), "configured")
        self.assertEqual(self.marker.read_bytes(), PENDING_MARKER)

        # A failed live smoke leaves PENDING_MARKER. The next deploy must not
        # treat activation as complete, even when the file is already correct.
        self.assertEqual(activate_once(self.env, self.marker), "already_configured")
        self.assertEqual(self.marker.read_bytes(), PENDING_MARKER)

        self.assertEqual(finalize_marker(self.marker), "finalized")
        self.assertEqual(self.marker.read_bytes(), CURRENT_MARKER)
        self.assertEqual(finalize_marker(self.marker), "already_finalized")

    def test_current_marker_makes_later_deploys_noop(self):
        self.env.write_text(f"{ENABLED_KEY}=false\n", encoding="utf-8")
        self.assertEqual(activate_once(self.env, self.marker), "configured")
        self.assertEqual(finalize_marker(self.marker), "finalized")
        self.assertEqual(self.marker.read_bytes(), CURRENT_MARKER)
        manually_disabled = (
            f"{ENABLED_KEY}=false\n"
            f"{RESOURCE_KEY}=https://manual.example/resource\n"
            f"{ISSUER_KEY}=https://manual.example/issuer\n"
        )
        self.env.write_text(manually_disabled, encoding="utf-8")
        self.assertEqual(activate_once(self.env, self.marker), "already_marked")
        self.assertEqual(self.env.read_text(encoding="utf-8"), manually_disabled)

    def test_finalize_requires_pending_marker(self):
        self.marker.parent.mkdir(parents=True)
        self.marker.write_bytes(LEGACY_MARKER)
        with self.assertRaisesRegex(RuntimeError, "not pending"):
            finalize_marker(self.marker)
        self.assertEqual(self.marker.read_bytes(), LEGACY_MARKER)

    def test_refuses_marker_symlink(self):
        self.env.write_text(f"{ENABLED_KEY}=false\n", encoding="utf-8")
        self.marker.parent.mkdir(parents=True)
        target = self.root / "marker-target"
        target.write_text("x\n", encoding="utf-8")
        self.marker.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "must not be a symlink"):
            activate_once(self.env, self.marker)

    def test_refuses_unknown_marker_version(self):
        self.env.write_text(f"{ENABLED_KEY}=false\n", encoding="utf-8")
        self.marker.parent.mkdir(parents=True)
        self.marker.write_text("unexpected\n", encoding="utf-8")
        original = self.env.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "unknown version"):
            activate_once(self.env, self.marker)
        self.assertEqual(self.env.read_bytes(), original)
