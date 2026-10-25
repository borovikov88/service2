"""Offline projection and fixed read-only transport checks; never touches Avito."""
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout

from scripts import avito_services_inventory_receipt as receipt

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("inventory_legacy_gate_test",
    ROOT / ".github/tests/test_avito_readonly_workflow.py")
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


def sample():
    return {"schema_version": 1, "kind": "avito_services_local_file_inventory",
        "connection_id": 4, "account_id": "446312402", "checked_at": "2026-10-25T02:50:00+00:00",
        "state": "blocked", "code": "file_check_needs_owner_original", "operation_state": "not_started",
        "draft_matches_published": True, "draft_revision": 15, "published_revision": 15,
        "asset_count": 0, "draft_referenced_asset_count": 0, "approved_shared_asset_count": 0,
        "cover_order_candidate_count": 0, "source_kind": "stored_normalized_jpeg",
        "raw_original_retained": False, "browser_clicks_verified": False,
        "external_http_checked": False, "external_generation_required": True}


class ProjectionTests(unittest.TestCase):
    def test_only_closed_metadata_survives_no_assets_paths_urls_or_capabilities(self):
        value = sample()
        value.update(path="/PRIVATE/path", url="https://private.invalid/PRIVATE", token="PRIVATE",
            asset_uuid="PRIVATE", actor_id=999, source_sha256="PRIVATE", marker="PRIVATE")
        result = receipt.normalize(value)
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertNotIn("asset_uuid", result)
        self.assertEqual(result["asset_count"], 0)
        self.assertFalse(result["external_http_checked"])
        self.assertTrue(result["external_generation_required"])

    def test_unavailable_scope_counters_are_null_never_zero(self):
        value = sample()
        value.update(state="blocked", code="file_check_scope_invalid",
            draft_revision=None, published_revision=None, draft_matches_published=None)
        value.update({key: None for key in receipt.COUNTS})
        result = receipt.normalize(value)
        self.assertTrue(all(result[key] is None for key in receipt.COUNTS))

    def test_ready_metadata_is_not_a_file_operation_or_external_confirmation(self):
        value = sample()
        value.update(state="ready", code="", asset_count=1, draft_referenced_asset_count=1,
            approved_shared_asset_count=1, cover_order_candidate_count=1)
        result = receipt.normalize(value)
        self.assertEqual(result["state"], "ready")
        self.assertFalse(result["browser_clicks_verified"])
        self.assertFalse(result["external_http_checked"])
        self.assertTrue(result["external_generation_required"])

    def test_wrong_scope_write_kind_false_proof_and_invalid_counters_fail_closed(self):
        mutations = (
            {"connection_id": 1}, {"connection_id": True}, {"account_id": "111"},
            {"kind": "avito_services_file_input_check"},
            {"raw_original_retained": True}, {"browser_clicks_verified": True},
            {"external_http_checked": True}, {"external_generation_required": False},
            {"asset_count": -1}, {"asset_count": True}, {"asset_count": None},
            {"asset_count": 1, "draft_referenced_asset_count": 2},
            {"state": "ready", "code": ""}, {"code": "PRIVATE"},
        )
        for changes in mutations:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                receipt.normalize(sample() | changes)

    def test_pending_generation_never_becomes_ready_from_candidate_count(self):
        value = sample()
        value.update(asset_count=1, draft_referenced_asset_count=1, approved_shared_asset_count=1,
            cover_order_candidate_count=1, operation_state="pending", code="file_check_result_unknown")
        self.assertEqual(receipt.normalize(value)["state"], "blocked")
        value.update(state="ready", code="")
        with self.assertRaises(ValueError):
            receipt.normalize(value)

    def test_schema_or_transport_failure_is_unavailable_not_an_empty_inventory(self):
        for raw in (b"PRIVATE ERROR", json.dumps(sample()).encode() + b"\n" + json.dumps(sample()).encode()):
            with tempfile.NamedTemporaryFile() as source:
                source.write(raw)
                source.flush()
                output = io.StringIO()
                with redirect_stdout(output):
                    status = receipt.main([source.name])
                self.assertEqual(status, 2)
                self.assertEqual(output.getvalue(), receipt.UNAVAILABLE + "\n")
                self.assertNotIn(receipt.RESULT, output.getvalue())

    def test_valid_single_inventory_prints_its_independent_result_frame(self):
        with tempfile.NamedTemporaryFile() as source:
            source.write(json.dumps(sample()).encode())
            source.flush()
            output = io.StringIO()
            with redirect_stdout(output):
                status = receipt.main([source.name])
        self.assertEqual(status, 0)
        self.assertTrue(output.getvalue().startswith(receipt.RESULT))


class RemoteTests(unittest.TestCase):
    def setUp(self):
        legacy.RemoteTests.setUp(self)
        python = self.base / "venv/bin/python"
        python.write_text(python.read_text().replace(
            "manage.py audit_avito_readonly --connection-id 4 --account-id 446312402 --revision 15",
            "manage.py run_avito_services_filecheck --connection-id 4 --account-id 446312402"))

    def run_remote(self, **updates):
        return subprocess.run(["bash", str(ROOT / ".github/scripts/avito_services_inventory_remote.sh"),
            legacy.SHA, str(self.app)], env=self.env | updates,
            capture_output=True, text=True, timeout=10)

    def test_only_fixed_default_inventory_command_is_invoked(self):
        result = self.run_remote()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        script = (ROOT / ".github/scripts/avito_services_inventory_remote.sh").read_text()
        self.assertNotIn("--perform", script)
        self.assertNotIn("SERVICE2_AVITO_FILECHECK", script)
        self.assertNotIn("send_avito_outbox", script)
        self.assertNotIn("recover_avito_original_reply", script)

    def test_stale_dirty_and_foreign_checkout_block_before_command(self):
        for values in ({"FAKE_HEAD": "b" * 40}, {"FAKE_REMOTE": "b" * 40},
                       {"FAKE_DIRTY": "1"}, {"FAKE_ORIGIN": "https://attacker.invalid/repo.git"}):
            with self.subTest(values=values):
                result = self.run_remote(**values)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.marker.exists())

    def test_shared_worker_read_lock_allows_inventory_but_exclusive_deployment_blocks(self):
        lock = self.base / "tmp/service2-deploy.lock"
        with lock.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
            self.assertEqual(self.run_remote().returncode, 0)
        self.marker.unlink()
        with lock.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertNotEqual(self.run_remote().returncode, 0)
            self.assertFalse(self.marker.exists())

    def test_symlink_or_unsafe_mutable_directory_blocks(self):
        lock = self.base / "tmp/service2-deploy.lock"
        outside = self.base / "outside"
        outside.touch()
        lock.symlink_to(outside)
        self.assertNotEqual(self.run_remote().returncode, 0)
        self.assertFalse(self.marker.exists())
        lock.unlink()
        (self.base / "tmp").chmod(0o770)
        self.assertNotEqual(self.run_remote().returncode, 0)
        self.assertFalse(self.marker.exists())


class WorkflowTests(unittest.TestCase):
    def test_inventory_step_precedes_api_and_reports_its_failure_independently(self):
        workflow = (ROOT / ".github/workflows/avito-readonly-audit.yml").read_text()
        inventory_start = workflow.index("name: Read the Services local-photo database inventory only")
        api_start = workflow.index("name: Read only services connection4/account446312402")
        self.assertLess(inventory_start, api_start)
        inventory = workflow[inventory_start:api_start]
        self.assertIn("id: local_photo_inventory", inventory)
        self.assertIn("continue-on-error: true", inventory)
        self.assertIn("steps.local_photo_inventory.outcome", inventory)
        self.assertIn("avito_services_inventory_remote.sh", inventory)
        self.assertIn("scripts/avito_services_inventory_receipt.py", inventory)
        self.assertNotIn("--perform", inventory)
        self.assertNotIn("SERVICE2_AVITO_FILECHECK", inventory)
        self.assertNotIn("send_avito_outbox", inventory)
        self.assertNotIn("recover_avito_original_reply", inventory)
        self.assertNotIn("continue-on-error:", workflow[api_start:])
        self.assertIn("avito_readonly_gate.py", workflow[:inventory_start])
        self.assertIn("environment: production", workflow[:inventory_start])
        self.assertIn("preflight", inventory)


if __name__ == "__main__":
    unittest.main()
