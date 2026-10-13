"""Offline publication/receipt guards; no hosting or application settings."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("receipt", ROOT / "scripts/avito_readonly_receipt.py")
RECEIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIPT)

class AvitoReadonlyWorkflowTests(unittest.TestCase):
    def test_operator_audit_is_opt_in_after_deploy_health_and_uses_same_lock(self):
        source = (ROOT / ".github/workflows/ci-deploy.yml").read_text()
        position = source.index("      - name: Read explicitly requested Avito")
        self.assertGreater(position, source.index("      - name: Verify deployed site health"))
        section = source[position:source.index("  calculator:", position)]
        self.assertIn("github.event_name == 'push' && contains(github.event.head_commit.message, '[avito-audit]')", section)
        self.assertIn("../tmp/service2-deploy.lock", section)
        self.assertIn("flock -n 9", section)
        self.assertIn('test "$(git rev-parse HEAD)" = %q', section)
        self.assertIn("StrictHostKeyChecking=yes", section)
        self.assertIn("timeout 360", section)
        self.assertIn('2>"$audit_temp/stderr"', section)
        self.assertNotIn("cat ", section)

    def render(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stdout"
            path.write_text(text)
            output = io.StringIO()
            with patch.object(RECEIPT.sys, "argv", ["renderer", str(path)]), contextlib.redirect_stdout(output):
                self.assertEqual(RECEIPT.main(), 0)
            return output.getvalue()

    def test_frame_normalization_accepts_greeting_but_never_echoes_it(self):
        value = {"schema_version": 1, "status": "blocked", "checked_at": "2026-10-13T10:00:00+07:00",
                 "source": {"error": "source_unavailable"}, "reason": "audit_no_active_connections",
                 "connections": [], "SECRET": "PRIVATE"}
        output = self.render("PRIVATE greeting\n" + RECEIPT.FRAME + json.dumps(value) + "\n")
        self.assertIn("AVITO_READONLY_AUDIT_RESULT", output)
        self.assertNotIn("PRIVATE", output)

    def test_multiple_missing_malformed_or_oversize_frames_never_echo_raw_data(self):
        for value in ("PRIVATE", RECEIPT.FRAME + "PRIVATE\n", RECEIPT.FRAME + "{}\n" + RECEIPT.FRAME + "{}\n", "PRIVATE" * 40000):
            output = self.render(value)
            self.assertEqual(output.strip(), "AVITO_READONLY_AUDIT_UNAVAILABLE receipt_invalid")

if __name__ == "__main__":
    unittest.main()
