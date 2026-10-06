import os
import stat
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from scripts.enable_operations_mcp import (
    CURRENT_MARKER,
    ENABLED_KEY,
    ORGANIZATION_KEY,
    PENDING_MARKER,
    PRODUCTION_ORGANIZATION_ID,
    PRODUCTION_RESOURCE_URL,
    RESOURCE_KEY,
    activate_once,
    configure_production,
    finalize_marker,
)


class OperationsMcpActivationTests(SimpleTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env = self.root / ".env"
        self.marker = self.root / "tmp" / "operations-mcp-activated"

    def tearDown(self):
        self.temporary.cleanup()

    def expected_configuration(self):
        return (
            f"{ENABLED_KEY}=true\n"
            f"{RESOURCE_KEY}={PRODUCTION_RESOURCE_URL}\n"
            f"{ORGANIZATION_KEY}={PRODUCTION_ORGANIZATION_ID}\n"
        )

    def test_adds_only_non_secret_operations_configuration(self):
        self.env.write_text(
            "SECRET_KEY=do-not-print\nSITE_URL=https://example.test\n",
            encoding="utf-8",
        )
        os.chmod(self.env, 0o600)
        before = self.env.stat()

        self.assertTrue(configure_production(self.env))

        after = self.env.stat()
        self.assertEqual(
            self.env.read_text(encoding="utf-8"),
            "SECRET_KEY=do-not-print\nSITE_URL=https://example.test\n"
            + self.expected_configuration(),
        )
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_uid, before.st_uid)
        self.assertEqual(after.st_gid, before.st_gid)
        self.assertEqual(stat.S_IMODE(after.st_mode), 0o600)

    def test_replaces_wrong_values_and_is_idempotent(self):
        self.env.write_text(
            f"{ENABLED_KEY}=false\n"
            f"{RESOURCE_KEY}=https://wrong.example/mcp/operations\n"
            f"{ORGANIZATION_KEY}=999\n"
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

    def test_preserves_crlf_and_untouched_bytes(self):
        original = (
            b"SECRET_KEY=left\xffright\r\n"
            + f"  export {ENABLED_KEY} = false\r\n".encode("ascii")
            + f"{RESOURCE_KEY}=https://wrong.example/resource\r\n".encode("ascii")
            + f"{ORGANIZATION_KEY}=999\r\n".encode("ascii")
        )
        self.env.write_bytes(original)
        self.assertTrue(configure_production(self.env))
        expected = (
            b"SECRET_KEY=left\xffright\r\n"
            + f"{ENABLED_KEY}=true\r\n".encode("ascii")
            + f"{RESOURCE_KEY}={PRODUCTION_RESOURCE_URL}\r\n".encode("ascii")
            + f"{ORGANIZATION_KEY}={PRODUCTION_ORGANIZATION_ID}\r\n".encode("ascii")
        )
        self.assertEqual(self.env.read_bytes(), expected)

    def test_refuses_duplicate_setting(self):
        self.env.write_text(
            f"{ENABLED_KEY}=false\nexport {ENABLED_KEY}=true\n",
            encoding="utf-8",
        )
        original = self.env.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "defined more than once"):
            configure_production(self.env)
        self.assertEqual(self.env.read_bytes(), original)

    def test_pending_marker_requires_live_validation_before_finalization(self):
        self.env.write_text(f"{ENABLED_KEY}=false\n", encoding="utf-8")
        self.assertEqual(activate_once(self.env, self.marker), "configured")
        self.assertEqual(self.marker.read_bytes(), PENDING_MARKER)

        self.assertEqual(activate_once(self.env, self.marker), "already_configured")
        self.assertEqual(self.marker.read_bytes(), PENDING_MARKER)

        self.assertEqual(finalize_marker(self.marker), "finalized")
        self.assertEqual(self.marker.read_bytes(), CURRENT_MARKER)
        self.assertEqual(finalize_marker(self.marker), "already_finalized")

    def test_current_marker_makes_later_deploy_noop(self):
        self.env.write_text(f"{ENABLED_KEY}=false\n", encoding="utf-8")
        self.assertEqual(activate_once(self.env, self.marker), "configured")
        self.assertEqual(finalize_marker(self.marker), "finalized")

        manually_disabled = f"{ENABLED_KEY}=false\n"
        self.env.write_text(manually_disabled, encoding="utf-8")
        self.assertEqual(activate_once(self.env, self.marker), "already_marked")
        self.assertEqual(self.env.read_text(encoding="utf-8"), manually_disabled)
