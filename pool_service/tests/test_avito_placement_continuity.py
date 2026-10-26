"""Synthetic continuity and bounded audit tests; no live IDs, amounts or messages."""
from copy import deepcopy
from datetime import datetime, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch
from django.template.loader import render_to_string
from django.test import SimpleTestCase, TransactionTestCase
from pool_service.avito_placement_continuity import classify_finish, continuity_context
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


def calendar_row(value, **delta):
    return row(finish_time=None, finish_time_source="", finish_evidence=classify_finish(value), **delta)


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


class CalendarDeadlineSummaryTests(SimpleTestCase):
    def test_eleven_known_source_dates_include_eight_up_to_seven_days_without_claiming_instants(self):
        connection, feed = scope()
        now = datetime(2026, 10, 26, 13, 51, tzinfo=datetime_timezone.utc)
        rows = [calendar_row(value, checked_at="2026-10-26T12:49:56Z") for value in
            ["2026-10-30 12:34:56"] * 7 + ["2026-11-02"] + ["2026-11-18"] * 3]
        for index, saved in enumerate(rows):
            saved["Id"] = saved["observation"]["id"] = f"synthetic-{index}"
            saved["AvitoId"] = saved["observation"]["avito_id"] = str(100 + index)
        before = deepcopy(rows)
        result = continuity_context(connection, feed, rows, now=now)
        self.assertEqual(result["due_count"], 8)
        self.assertEqual(result["unknown_count"], 0)
        self.assertEqual(result["calendar_only_count"], 11)
        self.assertEqual([item["days_remaining"] for item in result["rows"]], [4] * 7 + [7] + [23] * 3)
        for item in result["rows"]:
            self.assertIsNone(item["finish_time"])
            self.assertIsNone(item["finish_source"])
            self.assertFalse(item["can_reactivate"])
            self.assertIsNone(item["additional_cost"])
        self.assertFalse(result["provider_request_performed"])
        self.assertFalse(result["automatic_reactivation"])
        self.assertEqual(rows, before)
        html = render_to_string("pool_service/avito/_placement_continuity.html", {"placement_continuity": result})
        self.assertIn("от сегодня до +7 календарных дней включительно: 8", html)
        self.assertIn("Статус или дата окончания неизвестны: 0", html)
        self.assertIn("без подтверждённого часового пояса и точного момента: 11", html)
        self.assertIn("Автоматическое восстановление заблокировано", html)

    def test_calendar_boundaries_include_today_and_seven_days_but_request_refresh_after_date(self):
        connection, feed = scope()
        for value, days, state, due in (("2026-01-10", 8, "timezone_unknown", 0),
                ("2026-01-09", 7, "due_soon", 1), ("2026-01-05", 3, "due_soon", 1),
                ("2026-01-03", 1, "due_soon", 1), ("2026-01-02", 0, "due_soon", 1),
                ("2026-01-01", -1, "refresh_required", 0)):
            with self.subTest(value=value):
                result = continuity_context(connection, feed, [calendar_row(value)], now=NOW)
                self.assertEqual(result["rows"][0]["days_remaining"], days)
                self.assertEqual(result["rows"][0]["state"], state)
                self.assertEqual(result["rows"][0]["status"], "active")
                self.assertEqual(result["due_count"], due)
                self.assertEqual(result["unknown_count"], 0)

    def test_aware_deadlines_keep_barnaul_calendar_and_exact_expiry_behavior(self):
        connection, feed = scope()
        for value, days, state, due in (("2026-01-09T17:00:00Z", 8, "active", 0),
                ("2026-01-09T16:59:59Z", 7, "due_soon", 1),
                ("2026-01-05T03:00:00Z", 3, "due_soon", 1),
                ("2026-01-03T03:00:00Z", 1, "due_soon", 1),
                ("2026-01-02T03:00:01Z", 0, "due_soon", 1),
                ("2026-01-02T03:00:00Z", 0, "refresh_required", 0),
                ("2026-01-01T03:00:00Z", -1, "refresh_required", 0)):
            with self.subTest(value=value):
                result = continuity_context(connection, feed, [row(finish_time=value)], now=NOW)
                self.assertEqual(result["rows"][0]["days_remaining"], days)
                self.assertEqual(result["rows"][0]["state"], state)
                self.assertEqual(result["due_count"], due)
                self.assertEqual(result["unknown_count"], 0)
                self.assertEqual(result["calendar_only_count"], 0)

    def test_unusable_dates_or_untrusted_observations_do_not_create_calendar_deadlines(self):
        connection, feed = scope()
        for value in (None, "", "invalid", "2026-02-30", "2026-02-30 12:00:00", True, {}):
            with self.subTest(value=value):
                result = continuity_context(connection, feed, [calendar_row(value)], now=NOW)
                self.assertEqual(result["due_count"], 0)
                self.assertEqual(result["unknown_count"], 1)
                self.assertEqual(result["calendar_only_count"], 0)
                self.assertIsNone(result["rows"][0]["days_remaining"])
        saved = calendar_row("2026-01-06")
        for delta in ({"source": "unbound_cache"}, {"status": "unknown"}, {"id": "other"},
                {"finish_time": "invalid"}, {"finish_evidence": {"source": "foreign.finish_time",
                    "finish_state": "calendar_date_only", "source_finish_date": "2026-01-06"}},
                {"checked_at": "2026-01-03T03:00:00Z"}):
            with self.subTest(delta=delta):
                bad = deepcopy(saved)
                bad["observation"].update(delta)
                result = continuity_context(connection, feed, [bad], now=NOW)
                self.assertEqual(result["due_count"], 0)
                self.assertEqual(result["unknown_count"], 1)
                self.assertEqual(result["calendar_only_count"], 0)

    def test_calendar_dates_do_not_override_nonactive_status_or_owner_prohibition(self):
        connection, feed = scope()
        for status in ("old", "removed", "blocked", "rejected"):
            result = continuity_context(connection, feed, [calendar_row("2026-01-06", status=status)], now=NOW)
            self.assertEqual(result["due_count"], 0)
            self.assertEqual(result["rows"][0]["status"], status)
        saved = calendar_row("2026-01-06")
        saved["archive_control"]["owner_withdrawn"] = True
        result = continuity_context(connection, feed, [saved], now=NOW)
        self.assertEqual(result["due_count"], 0)
        self.assertEqual(result["rows"][0]["state"], "owner_withdrawn")


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


class FinishTimeShapeTests(SimpleTestCase):
    def test_naive_calendar_is_known_without_inventing_an_instant_or_timezone(self):
        from pool_service.avito_placement_continuity import classify_finish
        for value in ("2026-01-06T12:34:56", "2026-01-06 12:34:56"):
            result = classify_finish(value)
            self.assertEqual(result["finish_state"], "timezone_missing")
            self.assertEqual(result["source_finish_date"], "2026-01-06")
            self.assertIsNone(result["finish_time"])
            self.assertEqual(result["shape"]["format"], "naive_datetime")
            self.assertEqual(result["shape"]["stage"], "reader_normalized")
            self.assertNotIn(value, str(result))

    def test_aware_dateonly_and_unknown_offset_are_distinct(self):
        from pool_service.avito_placement_continuity import classify_finish
        aware = classify_finish("2026-01-06T12:34:56+07:00")
        self.assertEqual(aware["finish_state"], "known")
        self.assertTrue(aware["finish_time"].endswith("+07:00"))
        date_only = classify_finish("2026-01-06")
        self.assertEqual(date_only["finish_state"], "calendar_date_only")
        self.assertEqual(date_only["source_finish_date"], "2026-01-06")
        self.assertIsNone(date_only["finish_time"])
        unknown_offset = classify_finish("2026-01-06T12:34:56-00:00")
        self.assertEqual(unknown_offset["finish_state"], "timezone_missing")
        self.assertEqual(unknown_offset["shape"]["format"], "unknown_offset_datetime")
        self.assertIsNone(unknown_offset["finish_time"])

    def test_absent_types_invalid_values_and_overlong_text_never_escape(self):
        from pool_service.avito_placement_continuity import classify_finish
        for value, state in ((None, "not_returned"), ("", "not_returned"), (True, "invalid_type"),
                             (12, "invalid_type"), ({}, "invalid_type"), ([], "invalid_type"),
                             ("2026-02-30T12:00:00", "invalid_format"), ("PRIVATE token", "invalid_format"),
                             ("PRIVATE" * 100, "invalid_format")):
            result = classify_finish(value)
            self.assertEqual(result["finish_state"], state)
            self.assertIsNone(result["finish_time"])
            self.assertIsNone(result["source_finish_date"])
            self.assertNotIn("PRIVATE", str(result))

    def test_audit_keeps_source_calendar_but_does_not_increase_known_instants(self):
        connection, feed = scope()
        with patch.object(audit, "fetch_detail", return_value={"status": "active",
                "autoload_item_id": "synthetic", "finish_time": "2026-01-06 12:34:56"}):
            value = audit.services_placement_evidence(connection, "synthetic", "446312402", feed, {"101": {"status": "active"}})
        result = receipt.placement_evidence(value)
        self.assertEqual(result["terms_known"], 0)
        self.assertEqual(result["rows"][0]["source_finish_date"], "2026-01-06")
        self.assertIsNone(result["rows"][0]["finish_time"])
        self.assertEqual(result, receipt.placement_evidence(result))
        self.assertNotIn("12:34:56", str(result))

    def test_editor_source_date_is_counted_without_inventing_an_expiry_instant(self):
        from pool_service.avito_placement_continuity import classify_finish
        connection, feed = scope()
        saved = row(finish_time=None, finish_time_source="")
        saved["observation"]["finish_evidence"] = classify_finish("2026-01-06 12:34:56")
        result = continuity_context(connection, feed, [saved], now=NOW)
        self.assertEqual(result["rows"][0]["state"], "due_soon")
        self.assertEqual(result["rows"][0]["source_finish_date"].isoformat(), "2026-01-06")
        self.assertIsNone(result["rows"][0]["finish_time"])
        self.assertEqual(result["rows"][0]["days_remaining"], 4)
        self.assertEqual(result["due_count"], 1)
        self.assertEqual(result["unknown_count"], 0)
        self.assertEqual(result["calendar_only_count"], 1)

    def test_receipt_rejects_incoherent_shape_and_unsafe_calendar_values(self):
        connection, feed = scope()
        with patch.object(audit, "fetch_detail", return_value={"status": "active",
                "autoload_item_id": "synthetic", "finish_time": "2026-01-06 12:34:56"}):
            value = audit.services_placement_evidence(connection, "synthetic", "446312402", feed, {"101": {"status": "active"}})
        for changes in ({"source_finish_date": "PRIVATE"}, {"source_finish_date": "2026-02-30"},
                        {"finish_state": "known"}, {"source_finish_date": None}):
            bad = deepcopy(value)
            bad["rows"][0].update(changes)
            with self.assertRaises(ValueError):
                receipt.placement_evidence(bad)
        bad = deepcopy(value)
        bad["rows"][0]["finish_shape"]["format"] = "PRIVATE"
        with self.assertRaises(ValueError):
            receipt.placement_evidence(bad)



class InventoryCalendarForwardingTests(SimpleTestCase):
    def test_existing_inventory_checker_preserves_only_scoped_calendar_evidence(self):
        from pool_service.avito_services_feed import verify_inventory
        connection, feed = scope()
        detail = {"status": "active", "autoload_item_id": "synthetic", "finish_time": "2026-01-06 12:34:56"}
        with patch("pool_service.communication_avito.authorized_account_id", return_value="446312402"), \
             patch("pool_service.communication_avito.access_token", return_value="synthetic"), \
             patch.object(audit.avito_workspace, "_get", return_value={"resources": [{"id": 101}]}), \
             patch.object(audit, "fetch_detail", return_value=detail):
            result = verify_inventory(connection, feed.draft)
        observed = result["observations"][0]
        self.assertIsNone(observed["finish_time"])
        self.assertEqual(observed["finish_time_source"], "")
        self.assertEqual(observed["finish_evidence"]["source_finish_date"], "2026-01-06")
        self.assertEqual(observed["finish_evidence"]["finish_state"], "timezone_missing")
        self.assertNotIn("12:34:56", str(observed))


class OwnedObservationPersistenceTests(TransactionTestCase):
    def setUp(self):
        from django.contrib.auth.models import User
        from pool_service.models import Organization, OrganizationAccess
        from pool_service.communication_models import ChannelConnection, CommunicationChannel, CommunicationAccess, AvitoCredential
        from pool_service.avito_feed_models import AvitoServicesFeed
        from pool_service.tests.test_avito_services_feed import parse_export, export_bytes
        self.actor = User.objects.create_user("synthetic-calendar-owner")
        self.organization = Organization.objects.create(name="Synthetic calendar")
        OrganizationAccess.objects.create(user=self.actor, organization=self.organization, role="owner")
        CommunicationAccess.objects.update_or_create(user=self.actor, organization=self.organization,
            defaults={"can_manage_channels": True})
        channel = CommunicationChannel.objects.create(organization=self.organization, kind="avito", name="Synthetic")
        self.connection = ChannelConnection.objects.create(pk=4, channel=channel, external_id="446312402", name="Services")
        AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted="synthetic", client_secret_encrypted="synthetic")
        snapshot = parse_export(export_bytes())
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection, account_id="446312402",
            organization_id_at_import=self.organization.pk, draft=snapshot, published=snapshot,
            enabled=True, revision=2, published_revision=2)
        self.original = (deepcopy(self.feed.draft), deepcopy(self.feed.published), self.feed.revision, self.feed.published_revision)

    def observed(self, *_args, **_kwargs):
        from django.db import connection
        from django.utils import timezone
        from pool_service.avito_placement_continuity import classify_finish
        self.assertFalse(connection.in_atomic_block)
        return {"observations": [{"id": "111", "avito_id": "111", "status": "active",
            "status_label": "Active", "included": True, "exclusion_reason": "",
            "finish_time": None, "finish_time_source": "",
            "finish_evidence": classify_finish("2026-01-06 12:34:56"),
            "checked_at": timezone.now().isoformat(), "source": "avito_item_detail"}]}

    def assert_content_unchanged(self):
        self.feed.refresh_from_db()
        self.assertEqual((self.feed.draft, self.feed.published, self.feed.revision, self.feed.published_revision), self.original)

    def test_shared_checker_runs_read_outside_atomic_and_saves_only_observation_history(self):
        from pool_service.avito_services_feed import verify_owned_observations
        with patch("pool_service.avito_services_feed.verify_inventory", side_effect=self.observed):
            result = verify_owned_observations(self.connection, self.actor, expected_revision=2)
        self.assertEqual(result["source_calendar_dates"], 1)
        self.assertEqual(result["known_instants"], 0)
        self.assertFalse(result["draft_changed"])
        self.assertFalse(result["published_changed"])
        self.assert_content_unchanged()
        self.assertIsNotNone(self.feed.last_checked_at)
        self.assertEqual(self.feed.latest_observations[0]["finish_evidence"]["finish_state"], "timezone_missing")
        event = self.feed.versions.get(pk=result["history_event_id"])
        self.assertEqual(event.action, "verify")
        self.assertEqual(event.actor_id, self.actor.pk)
        self.assertEqual(event.changes, [])

    def test_actor_revocation_and_binding_rotation_after_read_reject_save(self):
        from django.contrib.auth.models import User
        from django.core.exceptions import PermissionDenied
        from pool_service.communication_models import AvitoCredential
        from pool_service.avito_services_feed import verify_owned_observations, PublicationConflict
        def revoked(*args):
            value = self.observed()
            User.objects.filter(pk=self.actor.pk).update(is_active=False)
            return value
        with patch("pool_service.avito_services_feed.verify_inventory", side_effect=revoked):
            with self.assertRaises(PermissionDenied):
                verify_owned_observations(self.connection, self.actor, expected_revision=2)
        self.assert_content_unchanged()
        self.assertFalse(self.feed.versions.exists())
        User.objects.filter(pk=self.actor.pk).update(is_active=True)
        def rotated(*args):
            value = self.observed()
            AvitoCredential.objects.filter(connection=self.connection).update(client_secret_encrypted="synthetic-rotated")
            return value
        with patch("pool_service.avito_services_feed.verify_inventory", side_effect=rotated):
            with self.assertRaises(PublicationConflict):
                verify_owned_observations(self.connection, self.actor, expected_revision=2)
        self.assert_content_unchanged()
        self.assertFalse(self.feed.versions.exists())

    def test_newer_observation_and_wrong_revision_cannot_be_overwritten(self):
        from django.utils import timezone
        from pool_service.avito_feed_models import AvitoServicesFeed
        from pool_service.avito_services_feed import verify_owned_observations, PublicationConflict
        def newer(*args):
            value = self.observed()
            from datetime import timedelta
            AvitoServicesFeed.objects.filter(pk=self.feed.pk).update(last_checked_at=timezone.now()+timedelta(minutes=1))
            return value
        with patch("pool_service.avito_services_feed.verify_inventory", side_effect=newer):
            with self.assertRaises(PublicationConflict):
                verify_owned_observations(self.connection, self.actor, expected_revision=2)
        with patch("pool_service.avito_services_feed.verify_inventory") as read:
            with self.assertRaises(PublicationConflict):
                verify_owned_observations(self.connection, self.actor, expected_revision=1)
        read.assert_not_called()
        self.assert_content_unchanged()
        self.assertFalse(self.feed.versions.exists())

    def test_protected_prepare_is_once_per_binding_and_drops_private_marker_keys(self):
        from pool_service.avito_placement_continuity import prepare_once, CHECK_KEY
        with patch("pool_service.avito_services_workplace_setup._actor", return_value=self.actor), \
             patch("pool_service.avito_services_feed.verify_inventory", side_effect=self.observed) as read:
            first = prepare_once()
            self.assertEqual(first["state"], "verified")
            self.connection.refresh_from_db()
            settings = deepcopy(self.connection.settings)
            settings[CHECK_KEY]["receipt"]["PRIVATE"] = "PRIVATE"
            self.connection.settings = settings
            self.connection.save(update_fields=["settings"])
            again = prepare_once()
        self.assertEqual(again["state"], "already_verified")
        self.assertEqual(again["history_event_id"], first["history_event_id"])
        self.assertEqual(read.call_count, 1)
        self.assertNotIn("PRIVATE", str(again))
        self.assert_content_unchanged()

    def test_failed_read_is_pending_and_not_a_completed_receipt(self):
        from pool_service.avito_placement_continuity import prepare_once, CHECK_KEY
        with patch("pool_service.avito_services_workplace_setup._actor", return_value=self.actor), \
             patch("pool_service.avito_services_feed.verify_inventory", side_effect=audit.AvitoError("provider_cooldown")) as read:
            first = prepare_once()
            again = prepare_once()
        self.assertEqual(first["state"], "pending")
        self.assertEqual(first["code"], "provider_cooldown")
        self.assertEqual(again["code"], "placement_check_busy")
        self.assertEqual(read.call_count, 1)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings[CHECK_KEY]["state"], "checking")
        self.assertNotIn("receipt", self.connection.settings[CHECK_KEY])
        self.assert_content_unchanged()
        self.assertFalse(self.feed.versions.exists())

    def test_changed_or_malformed_lease_after_read_rejects_save_without_raw_error(self):
        from pool_service.avito_placement_continuity import prepare_once, CHECK_KEY
        from pool_service.communication_models import ChannelConnection
        def changed(*args):
            value = self.observed()
            current = ChannelConnection.objects.get(pk=4)
            settings = deepcopy(current.settings)
            settings[CHECK_KEY] = "PRIVATE malformed marker"
            current.settings = settings
            current.save(update_fields=["settings"])
            return value
        with patch("pool_service.avito_services_workplace_setup._actor", return_value=self.actor), \
             patch("pool_service.avito_services_feed.verify_inventory", side_effect=changed):
            result = prepare_once()
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["code"], "placement_check_changed")
        self.assertNotIn("PRIVATE", str(result))
        self.assert_content_unchanged()
        self.assertFalse(self.feed.versions.exists())

    def test_provider_shaped_private_error_is_reduced_to_closed_diagnostic_code(self):
        from pool_service.avito_placement_continuity import prepare_once
        with patch("pool_service.avito_services_workplace_setup._actor", return_value=self.actor), \
             patch("pool_service.avito_services_feed.verify_inventory",
                   side_effect=audit.AvitoError("provider_private_secret")):
            result = prepare_once()
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["code"], "audit_read_failed")
        self.assertNotIn("private_secret", str(result))
        self.assert_content_unchanged()
