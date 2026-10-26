"""Synthetic continuity and bounded audit tests; no live IDs, amounts or messages."""
from copy import deepcopy
from datetime import datetime, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase
from pool_service.avito_placement_continuity import continuity_context
from pool_service import avito_readonly_audit as audit
from scripts import avito_readonly_receipt as receipt

NOW = datetime(2026, 1, 2, 3, 0, tzinfo=datetime_timezone.utc)


def scope():
    channel = SimpleNamespace(kind="avito", is_active=True, organization_id=7)
    connection = SimpleNamespace(pk=4, external_id="446312402", is_active=True, channel=channel)
    feed = SimpleNamespace(connection_id=4, account_id="446312402", organization_id_at_import=7,
        draft=[{"rows": [{"Id": "synthetic", "AvitoId": "101"}]}])
    return connection, feed


def row(**delta):
    observation = {"avito_id": "101", "id": "synthetic", "status": "active",
        "source": "avito_item_detail", "checked_at": "2026-01-02T02:00:00Z",
        "finish_time": "2026-01-06T03:00:00Z",
        "finish_time_source": "avito_item_detail.finish_time"} | delta
    return {"Id": "synthetic", "AvitoId": "101", "observation": observation,
            "archive_control": {"owner_withdrawn": False}}


class PlacementContinuityTests(SimpleTestCase):
    def test_saved_deadline_is_visible_without_reactivation_or_cost_claim(self):
        connection, feed = scope()
        rows = [row()]
        before = deepcopy(rows)
        result = continuity_context(connection, feed, rows, now=NOW)
        self.assertFalse(result["provider_request_performed"])
        self.assertFalse(result["automatic_reactivation"])
        self.assertEqual(result["due_count"], 1)
        self.assertEqual(result["rows"][0]["days_remaining"], 4)
        self.assertEqual(result["rows"][0]["state"], "due_soon")
        self.assertIsNone(result["rows"][0]["additional_cost"])
        self.assertFalse(result["rows"][0]["can_reactivate"])
        self.assertEqual(rows, before)

    def test_expired_timestamp_does_not_infer_archive_and_unknown_does_not_become_zero(self):
        connection, feed = scope()
        old = continuity_context(connection, feed, [row(finish_time="2026-01-01T03:00:00Z")], now=NOW)
        self.assertEqual(old["rows"][0]["status"], "active")
        self.assertEqual(old["rows"][0]["state"], "refresh_required")
        missing = continuity_context(connection, feed, [row(finish_time=None)], now=NOW)
        self.assertIsNone(missing["rows"][0]["finish_time"])
        self.assertIsNone(missing["rows"][0]["days_remaining"])
        self.assertEqual(missing["unknown_count"], 1)

    def test_owner_prohibition_and_unconfirmed_reason_remain_distinct(self):
        connection, feed = scope()
        stopped = row(status="old")
        stopped["archive_control"]["owner_withdrawn"] = True
        self.assertEqual(continuity_context(connection, feed, [stopped], now=NOW)["rows"][0]["state"], "owner_withdrawn")
        for status, state in (("old", "archive_unknown"), ("removed", "removed"),
                              ("blocked", "blocked"), ("rejected", "rejected")):
            self.assertEqual(continuity_context(connection, feed, [row(status=status)], now=NOW)["rows"][0]["state"], state)

    def test_other_connection_org_and_account_are_out_of_scope(self):
        for mutate in (lambda c,f: setattr(c, "pk", 1),
                       lambda c,f: setattr(c, "external_id", "999"),
                       lambda c,f: setattr(f, "organization_id_at_import", 8),
                       lambda c,f: setattr(f, "connection_id", 8),
                       lambda c,f: setattr(c.channel, "is_active", False)):
            connection, feed = scope()
            mutate(connection, feed)
            self.assertIsNone(continuity_context(connection, feed, [row()], now=NOW))

    def test_wrong_identity_untrusted_source_future_or_naive_check_is_unknown(self):
        connection, feed = scope()
        for delta in ({"avito_id": "999"}, {"id": "other"}, {"source": "unbound_cache"},
                      {"checked_at": "2026-01-03T03:00:00Z"}, {"checked_at": "2026-01-02T03:00:00"}):
            result = continuity_context(connection, feed, [row(**delta)], now=NOW)
            self.assertIsNone(result["rows"][0]["status"])
            self.assertIsNone(result["rows"][0]["finish_time"])


class FreshPlacementEvidenceTests(SimpleTestCase):
    def collect(self, detail=None, scan=None):
        connection, feed = scope()
        detail = detail or {"status": "active", "autoload_item_id": "synthetic",
            "finish_time": "2026-01-06T03:00:00Z", "title": "PRIVATE"}
        with patch.object(audit, "fetch_detail", return_value=detail) as fetch:
            value = audit.services_placement_evidence(connection, "synthetic-token", "446312402", feed,
                {"101": {"status": "active"}} if scan is None else scan)
        return value, fetch

    def test_owned_detail_receipt_is_complete_and_contains_no_private_values(self):
        value, fetch = self.collect()
        fetch.assert_called_once()
        projected = receipt.placement_evidence(value | {"token": "PRIVATE"})
        self.assertEqual(projected["requested"], 1)
        self.assertEqual(projected["checked"], 1)
        self.assertEqual(projected["terms_known"], 1)
        self.assertTrue(projected["complete"])
        self.assertFalse(projected["provider_write_performed"])
        self.assertFalse(projected["local_snapshot_changed"])
        self.assertNotIn("PRIVATE", str(projected))
        self.assertEqual(projected, receipt.placement_evidence(projected))

    def test_missing_finish_and_missing_xml_identity_are_not_fabricated(self):
        value, _ = self.collect({"status": "active", "autoload_item_id": "", "finish_time": ""})
        projected = receipt.placement_evidence(value)
        self.assertTrue(projected["complete"])
        self.assertEqual(projected["terms_known"], 0)
        self.assertEqual(projected["rows"][0]["identity_state"], "not_returned")
        self.assertIsNone(projected["rows"][0]["finish_time"])
        self.assertEqual(projected["rows"][0]["finish_state"], "not_returned")

    def test_midscan_status_or_identity_drift_is_a_row_error(self):
        for detail, expected in (({"status": "old", "autoload_item_id": "synthetic"}, "audit_placement_status_changed"),
                                  ({"status": "active", "autoload_item_id": "another"}, "provider_account_mismatch")):
            value, _ = self.collect(detail)
            projected = receipt.placement_evidence(value)
            self.assertFalse(projected["complete"])
            self.assertEqual(projected["checked"], 0)
            self.assertEqual(projected["rows"][0]["error"], expected)

    def test_foreign_account_and_oversize_scope_never_make_provider_call(self):
        connection, feed = scope()
        with patch.object(audit, "fetch_detail") as fetch:
            with self.assertRaises(audit.AvitoError):
                audit.services_placement_evidence(connection, "synthetic", "999", feed, {})
            feed.draft = [{"rows": [{"Id": str(i), "AvitoId": str(i)} for i in range(21)]}]
            with self.assertRaises(audit.AvitoError):
                audit.services_placement_evidence(connection, "synthetic", "446312402", feed, {})
        fetch.assert_not_called()

    def test_bad_count_naive_dates_and_write_claim_are_rejected(self):
        value, _ = self.collect()
        for delta in ({"checked": 0}, {"terms_known": 0}, {"provider_write_performed": True},
                      {"connection_id": 1}, {"account_id": "999"}):
            with self.assertRaises(ValueError):
                receipt.placement_evidence(value | delta)
        bad = deepcopy(value)
        bad["rows"][0]["finish_time"] = "2026-01-06T03:00:00"
        with self.assertRaises(ValueError):
            receipt.placement_evidence(bad)

    def test_time_budget_has_specific_safe_error_and_makes_no_late_request(self):
        connection, feed = scope()
        with patch.object(audit.time, "monotonic", side_effect=[0, 101]), \
             patch.object(audit, "fetch_detail") as fetch:
            value = audit.services_placement_evidence(connection, "synthetic", "446312402", feed, {})
        fetch.assert_not_called()
        self.assertEqual(receipt.placement_evidence(value)["rows"][0]["error"], "audit_placement_read_limit")
        for known in ("audit_placement_scope_invalid", "audit_placement_read_limit", "audit_placement_status_changed"):
            self.assertEqual(audit.safe_error(audit.AvitoError(known)), known)
        self.assertEqual(audit.safe_error(audit.AvitoError("PRIVATE raw error")), "audit_read_failed")
