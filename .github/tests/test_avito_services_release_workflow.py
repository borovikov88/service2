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
        self.env["FAKE_COMMIT_MESSAGE"] = "[avito-services-release-v15-20261024]"
        git = self.base / "bin/git"
        git.write_text(git.read_text().replace(
            "  *) exit 98 ;;",
            "  show) printf '%s\\n' \"$FAKE_COMMIT_MESSAGE\" ;;\n  *) exit 98 ;;"))

    def test_r2_dispatch_requires_both_actual_commit_markers(self):
        python = self.base / "venv/bin/python"
        source = python.read_text().replace(
            "run_avito_services_release_operation --expected-sha " + BASE.SHA +
            " --release-generation services-v15-20261024-r1",
            "run_avito_services_source_pin --expected-sha " + BASE.SHA +
            " --release-generation services-source-pin-r2-20261024")
        python.write_text(source)
        message = "[avito-services-release-v15-20261024] [avito-services-source-pin-r2-20261024]"
        result = self.run_remote(FAKE_COMMIT_MESSAGE=message)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self.marker.exists())

    def test_r2_marker_alone_fails_before_command(self):
        result = self.run_remote(FAKE_COMMIT_MESSAGE="[avito-services-source-pin-r2-20261024]")
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(self.marker.exists())

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




def source_pin_publication():
    return {"configured": True, "binding_present": True, "snapshot_valid": True,
        "current_publication": True, "draft_matches_published": True, "identity_sets_match": True,
        "photo_limit_ok": True, "http_checked": True, "snapshot_source": "published_history",
        "revision": 17, "draft_revision": 17, "published_revision": 17, "history_version_id": 42,
        "created_at": "2026-10-24T12:00:00+00:00", "checked_at": "2026-10-24T12:01:00+00:00",
        "draft_count": 11, "published_count": 11, "snapshot_count": 11, "max_photos": 10,
        "http_matches": None, "http_code": "http_unavailable",
        "facts": {"file_published": True, "upload_found": False, "source_confirmed": False,
                  "rows_processed": False, "live_content_verified": False}}


def historical_photo_proof():
    return {"connection_id": 4, "account_id": "446312402", "checked_at": "2026-10-24T11:59:00+00:00",
        "source": {"scope_valid": True, "source": "existing_services_draft_image",
            "source_in_published": True, "photo_count": 10, "readable": True, "format": "JPEG",
            "width": 20, "height": 10, "bytes": 100, "checked_at": "2026-10-24T11:58:00+00:00",
            "https_upgrade_followed": True},
        "upload_saved": True, "private_preview_verified": True, "cover_order_saved": True,
        "original_draft_restored": True, "published_unchanged": True,
        "image_publicly_available": False, "browser_clicks_verified": False, "provider_content_verified": False,
        "revision_before": 15, "revision_after": 17, "edit_version_id": 40, "restore_version_id": 41}


def source_pin_result():
    return {"schema_version": 1, "kind": "avito_services_source_pin",
        "generation": "services-source-pin-r2-20261024", "connection_id": 4, "account_id": "446312402",
        "source_revision": 15, "checked_at": "2026-10-24T12:01:00+00:00",
        "state": "published", "code": "source_pin_http_unconfirmed",
        "photo_before_publication": historical_photo_proof(), "publication": source_pin_publication(),
        "same_content": True, "all_ids_active": True, "excluded_count": 0}


class SourcePinProjectionTests(unittest.TestCase):
    def test_local_publication_with_unconfirmed_http_preserves_both_facts_without_republishing_claim(self):
        raw = source_pin_result()
        raw.update(private_url="PRIVATE", operation_id="PRIVATE")
        raw["publication"]["private_url"] = "PRIVATE"
        raw["photo_before_publication"]["operation_id"] = "PRIVATE"
        value = RECEIPT.normalize(raw)
        self.assertEqual("published", value["state"])
        self.assertTrue(value["publication"]["facts"]["file_published"])
        self.assertIsNone(value["publication"]["http_matches"])
        self.assertFalse(value["publication"]["facts"]["source_confirmed"])
        self.assertFalse(value["photo_before_publication"]["browser_clicks_verified"])
        self.assertTrue(value["photo_before_publication"]["source"]["https_upgrade_followed"])
        self.assertNotIn("PRIVATE", str(value))

    def test_incomplete_photo_or_changed_content_cannot_be_a_published_result(self):
        for changed in ({"photo_before_publication": None}, {"same_content": False},
                        {"all_ids_active": None}, {"excluded_count": 1},
                        {"generation": "services-v15-20261024-r1"},
                        {"account_id": "1001"}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                RECEIPT.normalize(source_pin_result() | changed)
        bound = source_pin_result() | {"state": "already_bound", "code": "source_pin_already_bound",
            "photo_before_publication": None, "same_content": None, "all_ids_active": None, "excluded_count": None}
        self.assertIsNone(RECEIPT.normalize(bound)["photo_before_publication"])

    def test_two_action_frames_or_frame_kind_mismatch_never_become_success(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stdout"
            for raw in (
                RECEIPT.FRAME + json.dumps(source_pin_result()) + "\n",
                RECEIPT.SOURCE_PIN_FRAME + json.dumps(source_pin_result()) + "\n" +
                    RECEIPT.FRAME + "{}\n",
            ):
                path.write_text(raw)
                output = io.StringIO()
                with patch.object(RECEIPT.sys, "argv", ["renderer", str(path)]), \
                     contextlib.redirect_stdout(output):
                    self.assertEqual(0, RECEIPT.main())
                self.assertEqual("AVITO_SERVICES_RELEASE_UNAVAILABLE receipt_invalid", output.getvalue().strip())

if __name__ == "__main__":
    unittest.main()
