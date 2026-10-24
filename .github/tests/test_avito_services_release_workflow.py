"""Offline protection tests for the one approved draft-photo operation."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
BASE_SPEC = importlib.util.spec_from_file_location(
    "readonly_workflow_tests", ROOT / ".github/tests/test_avito_readonly_workflow.py")
BASE = importlib.util.module_from_spec(BASE_SPEC)
BASE_SPEC.loader.exec_module(BASE)
SPEC = importlib.util.spec_from_file_location(
    "operation_receipt", ROOT / "scripts/avito_services_release_receipt.py")
RECEIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIPT)


class ReleaseRemoteTests(BASE.RemoteTests):
    def setUp(self):
        super().setUp()
        path = self.base / "venv/bin/python"
        source = path.read_text()
        source = source.replace(
            'manage.py audit_avito_readonly --connection-id 4 --account-id 446312402 --revision 15',
            'manage.py run_avito_services_release_operation --expected-sha ' + BASE.SHA +
            ' --release-generation services-v15-20261024-r1')
        source = source.replace('touch "$AVITO_TEST_MARKER"',
                                '[[ "$SERVICE2_AVITO_RELEASE_OPERATOR" == 1 ]]\ntouch "$AVITO_TEST_MARKER"')
        path.write_text(source)

    def run_remote(self, **updates):
        return subprocess.run(
            ["bash", str(ROOT / ".github/scripts/avito_services_release_remote.sh"),
             BASE.SHA, str(self.app)], env=self.env | updates,
            capture_output=True, text=True, timeout=10)


class ReleaseWorkflowGuardTests(unittest.TestCase):
    def test_only_explicit_marker_main_push_can_run_after_health_and_setup(self):
        source = (ROOT / ".github/workflows/ci-deploy.yml").read_text()
        start = source.index("      - name: Run explicitly approved services draft photo check")
        end = source.index("      - name:", start + 10)
        section = source[start:end]
        self.assertGreater(start, source.index("      - name: Verify deployed site health"))
        self.assertGreater(start, source.index("      - name: Set up authorised Avito services connection once"))
        self.assertIn("github.event_name == 'push'", section)
        self.assertIn("github.ref == 'refs/heads/main'", section)
        self.assertIn("[avito-services-release-v15-20261024]", section)
        self.assertIn('2>"$operation_temp/stderr"', section)
        self.assertIn("scripts/avito_services_release_receipt.py", section)
        self.assertNotIn("cat ", section)
        self.assertNotIn("configure_avito_services_workplace", section)
        remote = (ROOT / ".github/scripts/avito_services_release_remote.sh").read_text()
        self.assertIn("SERVICE2_AVITO_RELEASE_OPERATOR=1", remote)
        self.assertIn("timeout 360", remote)
        self.assertIn("services-v15-20261024-r1", remote)
        self.assertNotIn("services_feed_publish", remote)
        self.assertNotIn("force", remote)

    def test_renderer_drops_raw_greeting_and_rejects_missing_multiple_or_invalid_frames(self):
        value = {"schema_version": 1, "kind": "avito_services_release_operation",
            "generation": "services-v15-20261024-r1", "connection_id": 4, "account_id": "446312402",
            "expected_published_revision": 15, "checked_at": "2026-10-24T12:00:00+00:00",
            "state": "unknown", "code": "release_photo_unknown",
            "publication_preflight": None, "photo_source": None, "photo_proof": None,
            "private_token": "PRIVATE"}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stdout"
            for raw, valid in (
                ("PRIVATE greeting\n" + RECEIPT.FRAME + json.dumps(value) + "\n", True),
                ("PRIVATE", False),
                (RECEIPT.FRAME + "{}\n" + RECEIPT.FRAME + "{}\n", False),
                (RECEIPT.FRAME + json.dumps(value | {"account_id": "1001"}) + "\n", False),
                (RECEIPT.FRAME + json.dumps(value | {"state": "completed", "code": "release_completed"}) + "\n", False),
            ):
                path.write_text(raw)
                output = io.StringIO()
                with patch.object(RECEIPT.sys, "argv", ["renderer", str(path)]), \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(0, RECEIPT.main())
                self.assertNotIn("PRIVATE", output.getvalue())
                self.assertIn("OPERATION_RESULT" if valid else "receipt_invalid", output.getvalue())


if __name__ == "__main__":
    unittest.main()
