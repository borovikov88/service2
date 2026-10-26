"""The deployment prints a strict aggregate receipt, never untrusted SSH output."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("lead_receipt", ROOT / "scripts/crm_lead_intake_receipt.py")
receipt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receipt)


class ReceiptTests(unittest.TestCase):
    def value(self):
        return {"status": "applied", "expected_sha": "a" * 40, "run_id": 123, "run_attempt": 1,
            "approval_id": receipt.APPROVAL_ID, "eligible_manager_count": 2,
            "granted_manager_count": 2, "excluded_manager_count": 0,
            "pipelines_created": 0, "stages_created": 0,
            "crm_v2_read": True, "crm_v2_write": True, "crm_intake_process": True,
            "requester_access": True, "managers_access_verified": True,
            "starts_at": "2026-10-26T10:00:00+00:00"}

    def normalize(self, value):
        return receipt.normalize(receipt.PREFIX + json.dumps(value), "a" * 40, "123", "1")

    def test_exact_success_and_replay_without_restore(self):
        value = self.value()
        self.assertEqual(self.normalize(value), value)
        value.update(status="already_applied", granted_manager_count=0, crm_intake_process=False, managers_access_verified=False)
        self.assertEqual(self.normalize(value), value)

    def test_scope_commit_flag_type_or_private_payload_rejected(self):
        for change in ({"approval_id": "crm-aqualine-managers-20261026"}, {"expected_sha": "b" * 40},
                       {"run_id": 124}, {"crm_intake_process": False}, {"granted_manager_count": True},
                       {"phone": "synthetic-private"}, {"starts_at": "secret"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.normalize({**self.value(), **change})

    def test_untrusted_stderr_never_printed(self):
        self.assertNotIn("synthetic-private", receipt.failure("database payload synthetic-private"))


if __name__ == "__main__":
    unittest.main()
