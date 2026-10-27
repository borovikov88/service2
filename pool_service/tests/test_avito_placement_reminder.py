"""Synthetic local notifications and mocked owned reads only; no live delivery."""
import io
from copy import deepcopy
from datetime import datetime, timedelta, timezone as dtz
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command, CommandError
from django.test import TestCase

from pool_service import avito_placement_reminder as reminders
from pool_service.avito_feed_models import AvitoServicesFeed
from pool_service.avito_placement_continuity import classify_finish
from pool_service.avito_publication_models import AvitoServicesListingIntent
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import (
    AvitoListingStatus, AvitoStatusMonitor, ChannelConnection, CommunicationAccess, CommunicationChannel,
)
from pool_service.models import Notification, Organization, OrganizationAccess, Profile

NOW = datetime(2026, 1, 2, 3, tzinfo=dtz.utc)  # 10:00 Barnaul


class PlacementReminderTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic reminder organization")
        self.owner = User.objects.create_user("synthetic-placement-owner")
        self.manager = User.objects.create_user("synthetic-placement-manager")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        OrganizationAccess.objects.create(user=self.manager, organization=self.org, role="manager")
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Synthetic")
        self.connection = ChannelConnection.objects.create(pk=4, channel=self.channel, external_id="446312402")
        self.monitor = AvitoStatusMonitor.objects.create(connection=self.connection, organization=self.org,
            account_id="446312402", recipient=self.owner, enabled=True, next_due_at=NOW + timedelta(days=1))
        self.feed = AvitoServicesFeed.objects.create(connection=self.connection, account_id="446312402",
            organization_id_at_import=self.org.pk, draft=[{"rows": [{"Id": "synthetic", "AvitoId": "101"}]}])
        self.observe("2026-01-09")
        self.clock = patch.object(reminders.timezone, "now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def observe(self, finish, *, checked=NOW, **changes):
        evidence = classify_finish(finish)
        observation = {"id": "synthetic", "avito_id": "101", "source": "avito_item_detail",
            "checked_at": checked.isoformat(), "status": "active", "finish_time": evidence["finish_time"],
            "finish_time_source": "avito_item_detail.finish_time" if evidence["finish_time"] else "",
            "finish_evidence": evidence} | changes
        self.feed.latest_observations = [observation]
        self.feed.last_checked_at = checked
        self.feed.save(update_fields=["latest_observations", "last_checked_at"])

    def run_at(self, now=NOW):
        with patch.object(reminders.timezone, "now", return_value=now):
            return reminders.scan_saved()

    def test_calendar_stages_repeat_dedupe_and_separate_three_one(self):
        for day, stage in ((2, 7), (6, 3), (8, 1)):
            current = NOW.replace(day=day)
            self.observe("2026-01-09 23:59:59", checked=current)
            self.assertEqual(self.run_at(current), ("success", 1))
            self.assertEqual(self.run_at(current), ("success", 0))
            self.assertIn(f"порог {stage} дн.", Notification.objects.latest("pk").title)
        self.assertEqual(Notification.objects.count(), 3)
        self.assertEqual(set(Notification.objects.values_list("user_id", flat=True)), {self.owner.pk})
        self.assertTrue(all("без подтверждённого часового пояса" in x.message for x in Notification.objects.all()))

    def test_first_late_run_emits_only_nearest_threshold_and_no_historical_burst(self):
        for remaining, expected in ((8, None), (7, 7), (4, 7), (3, 3), (2, 3), (1, 1), (0, 1), (-1, None)):
            with self.subTest(remaining=remaining):
                Notification.objects.all().delete()
                self.connection.settings = {}
                self.connection.save(update_fields=["settings"])
                self.observe((NOW.date() + timedelta(days=remaining)).isoformat())
                self.assertEqual(self.run_at()[1], int(expected is not None))
                if expected:
                    self.assertIn(f"порог {expected} дн.", Notification.objects.get().title)
                    self.assertEqual(self.run_at()[1], 0)

    def test_new_date_and_confirmed_cycle_are_new_events_same_date_refresh_is_not(self):
        self.assertEqual(self.run_at()[1], 1)
        self.observe("2026-01-09", checked=NOW + timedelta(minutes=1))
        self.assertEqual(self.run_at(NOW + timedelta(minutes=1))[1], 0)
        self.observe("2026-01-08")
        self.assertEqual(self.run_at()[1], 1)
        self.observe("2026-01-09")
        self.assertEqual(self.run_at()[1], 1)
        self.observe("2026-01-09", reactivation={"confirmed": True,
            "source": "same_identity_provider_status_and_term", "confirmed_at": NOW.isoformat()})
        self.assertEqual(self.run_at()[1], 1)
        self.assertEqual(self.run_at()[1], 0)

    def test_exact_instants_use_barnaul_calendar_days_not_24_hour_intervals(self):
        self.observe("2026-01-08T18:00:00+00:00")  # Jan9 01:00 Barnaul
        self.assertEqual(self.run_at()[1], 1)
        self.assertIn("09.01.2026", Notification.objects.get().message)
        self.assertIn("осталось 7 календарных", Notification.objects.get().message)
        self.assertNotIn("без подтверждённого", Notification.objects.get().message)

    def test_inactive_and_owner_withdrawn_do_not_notify(self):
        for status in ("old", "removed", "blocked", "rejected"):
            self.observe("2026-01-09", status=status)
            self.assertEqual(self.run_at()[1], 0)
        self.observe("2026-01-09")
        AvitoServicesListingIntent.objects.create(feed=self.feed, avito_id="101", account_id="446312402",
            organization_id=self.org.pk, owner_withdrawn=True)
        self.assertEqual(self.run_at()[1], 0)
        self.assertFalse(Notification.objects.exists())

    def test_newer_inactive_status_overrides_saved_owned_active(self):
        AvitoListingStatus.objects.create(monitor=self.monitor, item_id="101", status="old", last_seen_at=NOW)
        self.assertEqual(self.run_at()[1], 0)

    def test_missing_untrusted_old_or_invalid_observations_are_not_dates_or_expiry(self):
        for changes in ({"source": "untrusted"}, {"id": "other"}, {"avito_id": "999"},
                {"checked_at": (NOW - timedelta(hours=25)).isoformat()},
                {"checked_at": (NOW + timedelta(minutes=1)).isoformat()},
                {"checked_at": "2026-01-02T03:00:00"}):
            self.observe("2026-01-09", **changes)
            self.assertEqual(self.run_at()[1], 0)
        for finish in (None, "garbage", "2026-02-30", {}, 0):
            self.observe(finish)
            self.assertEqual(self.run_at()[1], 0)
        self.observe("2026-01-09", finish_evidence={"source": "untrusted", "finish_state": "calendar_date_only",
            "source_finish_date": "2026-01-09", "finish_time": None})
        self.assertEqual(self.run_at()[1], 0)
        self.observe("2026-01-09", checked=NOW - timedelta(hours=25))
        self.assertEqual(self.run_at(), ("skipped", 0))
        self.assertFalse(Notification.objects.exists())

    def test_night_is_deferred_and_does_not_consume_stage(self):
        for hour in (0, 13, 14, 23):  # UTC -> 07,20,21,06 Barnaul
            current = NOW.replace(hour=hour)
            self.observe("2026-01-09", checked=current)
            expected = ("success", 1) if hour == 13 else ("skipped", 0)
            self.assertEqual(self.run_at(current), expected)
        Notification.objects.all().delete()
        self.connection.settings = {}
        self.connection.save(update_fields=["settings"])
        self.observe("2026-01-09", checked=NOW.replace(hour=0))
        self.assertEqual(self.run_at(NOW.replace(hour=0)), ("skipped", 0))
        self.assertEqual(self.run_at(NOW.replace(hour=1)), ("success", 1))  # 08:00

    def test_disabled_subscription_access_revocation_and_scope_fail_closed(self):
        self.monitor.enabled = False
        self.monitor.save()
        self.assertEqual(self.run_at(), ("skipped", 0))
        self.monitor.enabled = True
        self.monitor.save()
        CommunicationAccess.objects.filter(user=self.owner).update(can_manage_channels=False)
        self.assertEqual(self.run_at(), ("skipped", 0))
        CommunicationAccess.objects.filter(user=self.owner).update(can_manage_channels=True)
        for update in ({"account_id": "999"}, {"organization_id_at_import": self.org.pk + 1}):
            AvitoServicesFeed.objects.filter(pk=self.feed.pk).update(**update)
            self.assertEqual(self.run_at(), ("skipped", 0))
            self.feed.save()
        self.connection.is_active = False
        self.connection.save(update_fields=["is_active"])
        self.assertEqual(self.run_at(), ("skipped", 0))

    def test_goods_connection_one_and_account_drift_are_untouched(self):
        goods = ChannelConnection.objects.create(pk=1, channel=self.channel, external_id="123", settings={"keep": 1})
        self.connection.external_id = "999"
        self.connection.save(update_fields=["external_id"])
        self.assertEqual(self.run_at(), ("skipped", 0))
        goods.refresh_from_db()
        self.assertEqual(goods.settings, {"keep": 1})
        self.assertFalse(Notification.objects.exists())

    def test_opt_out_recovers_without_stage_consumption_and_push_never_runs(self):
        profile, _ = Profile.objects.get_or_create(user=self.owner)
        profile.in_app_notifications_enabled = False
        profile.save()
        with patch("pool_service.services.notifications.send_push_to_users") as push:
            self.assertEqual(self.run_at()[1], 0)
            profile.in_app_notifications_enabled = True
            profile.save()
            self.assertEqual(self.run_at()[1], 1)
            self.assertEqual(self.run_at()[1], 0)
        push.assert_not_called()

    def test_notifications_state_and_dedupe_commit_atomically(self):
        with patch.object(reminders, "notify_users", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                self.run_at()
        self.connection.refresh_from_db()
        self.assertNotIn(reminders.KEY, self.connection.settings)
        self.assertFalse(Notification.objects.exists())
        self.assertEqual(self.run_at()[1], 1)

    def test_refresh_reuses_existing_owned_verifier_guards_and_preserves_content(self):
        from pool_service.avito_services_feed import verify_owned_observations
        before = deepcopy((self.feed.draft, self.feed.published, self.feed.revision, self.feed.published_revision))
        self.observe("2026-01-09", checked=NOW - timedelta(hours=13))
        refreshed = deepcopy(self.feed.latest_observations)
        refreshed[0]["checked_at"] = NOW.isoformat()
        with patch("pool_service.avito_services_feed.verify_inventory", return_value={"observations": refreshed}) as reader:
            def verify(*args, **kwargs):
                return verify_owned_observations(*args, **kwargs)
            with patch("pool_service.avito_services_feed.verify_owned_observations", side_effect=verify) as verifier:
                self.assertEqual(reminders.scan_saved(deadline=reminders.time.monotonic() + 90)[1], 1)
        reader.assert_called_once()
        verifier.assert_called_once()
        self.feed.refresh_from_db()
        self.assertEqual((self.feed.draft, self.feed.published, self.feed.revision, self.feed.published_revision), before)
        self.assertEqual(self.feed.versions.filter(action="verify").count(), 1)
        self.assertEqual(self.feed.last_checked_at, NOW)

    def test_refresh_not_repeated_fresh_not_at_night_or_without_budget(self):
        with patch("pool_service.avito_services_feed.verify_owned_observations") as verify:
            reminders.scan_saved(deadline=reminders.time.monotonic() + 90)
            self.observe("2026-01-09", checked=NOW - timedelta(hours=25))
            self.assertEqual(reminders.scan_saved(deadline=reminders.time.monotonic() + 59), ("skipped", 0))
            with patch.object(reminders.timezone, "now", return_value=NOW.replace(hour=14)):
                reminders.scan_saved(deadline=reminders.time.monotonic() + 90)
        verify.assert_not_called()

    def test_refresh_guards_reject_disable_or_binding_change_during_read(self):
        self.observe("2026-01-09", checked=NOW - timedelta(hours=25))
        def changed(current, actor, **kwargs):
            AvitoStatusMonitor.objects.filter(pk=self.monitor.pk).update(enabled=False)
            self.assertFalse(kwargs["expected_guard"](current, self.feed))
        with patch("pool_service.avito_services_feed.verify_owned_observations", side_effect=changed):
            self.assertEqual(reminders.scan_saved(deadline=reminders.time.monotonic() + 90), ("skipped", 0))
        self.assertFalse(Notification.objects.exists())

    def test_refresh_failure_keeps_valid_saved_delivery_and_dedupe_but_reports_failure(self):
        self.observe("2026-01-09", checked=NOW - timedelta(hours=13))
        for error in ("provider_unavailable PRIVATE", "provider_http_429 PRIVATE"):
            with self.subTest(error=error), patch("pool_service.avito_services_feed.verify_owned_observations",
                    side_effect=AvitoError(error)):
                expected = 1 if Notification.objects.count() == 0 else 0
                self.assertEqual(reminders.scan_saved(deadline=reminders.time.monotonic() + 90), ("failed", expected))
        self.assertEqual(Notification.objects.count(), 1)
        self.connection.refresh_from_db()
        state = self.connection.settings[reminders.KEY]
        self.assertTrue(state["last_refresh_failed"])
        self.assertNotIn("PRIVATE", str(state))
        self.assertNotIn("PRIVATE", Notification.objects.get().message)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.last_checked_at, NOW - timedelta(hours=13))

    def test_refresh_failure_cannot_use_stale_evidence_and_worker_reports_partial_delivery(self):
        zero = {"success": 0, "failed": 0, "skipped": 0}
        with patch("pool_service.avito_services_feed.verify_owned_observations", side_effect=AvitoError("PRIVATE")), \
                patch("pool_service.avito_tariff_reminder.scan_due", return_value=zero), \
                patch("pool_service.avito_advance.scan_due", return_value=zero), \
                patch("pool_service.avito_autoload_auto.scan_due", return_value=zero), \
                patch("pool_service.avito_daily_statistics.scan_due", return_value=zero):
            self.observe("2026-01-09", checked=NOW - timedelta(hours=25))
            self.assertEqual(reminders.scan_saved(deadline=reminders.time.monotonic() + 90), ("failed", 0))
            self.assertFalse(Notification.objects.exists())
            self.observe("2026-01-09", checked=NOW - timedelta(hours=13))
            output = io.StringIO()
            with self.assertRaises(CommandError) as raised:
                call_command("monitor_avito_statuses", stdout=output)
            self.assertEqual(raised.exception.returncode, 2)
            self.assertIn("AVITO_PLACEMENT_REMINDER outcome=failed notifications=1", output.getvalue())
            self.assertNotIn("PRIVATE", output.getvalue())
            self.assertEqual(Notification.objects.count(), 1)

    def test_temporary_draft_absence_preserves_cycle_and_later_threshold(self):
        self.assertEqual(self.run_at()[1], 1)
        self.connection.refresh_from_db()
        generation = self.connection.settings[reminders.KEY]["listings"]["101"]["generation"]
        original_draft, original_observations = deepcopy(self.feed.draft), deepcopy(self.feed.latest_observations)
        self.feed.draft = [{"rows": [{"Id": "synthetic-other", "AvitoId": "102"}]}]
        self.feed.latest_observations = [dict(original_observations[0], id="synthetic-other", avito_id="102",
            finish_evidence=classify_finish("2026-02-09"))]
        self.feed.save(update_fields=["draft", "latest_observations"])
        self.assertEqual(self.run_at(), ("success", 0))
        self.feed.draft, self.feed.latest_observations = original_draft, original_observations
        self.feed.save(update_fields=["draft", "latest_observations"])
        self.assertEqual(self.run_at(), ("success", 0))
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.settings[reminders.KEY]["listings"]["101"]["generation"], generation)
        current = NOW.replace(day=6)
        self.observe("2026-01-09", checked=current)
        self.assertEqual(self.run_at(current), ("success", 1))
        self.assertIn("порог 3 дн.", Notification.objects.latest("pk").title)
        self.assertEqual(Notification.objects.count(), 2)

    def test_dedupe_capacity_fails_closed_without_evicting_known_cycle(self):
        self.assertEqual(self.run_at()[1], 1)
        self.connection.refresh_from_db()
        original_state = deepcopy(self.connection.settings[reminders.KEY]["listings"]["101"])
        original_draft, original_observations = deepcopy(self.feed.draft), deepcopy(self.feed.latest_observations)
        self.feed.draft = [{"rows": [{"Id": "synthetic-other", "AvitoId": "102"}]}]
        self.feed.latest_observations = [dict(original_observations[0], id="synthetic-other", avito_id="102")]
        self.feed.save(update_fields=["draft", "latest_observations"])
        with patch.object(reminders, "MAX_TRACKED_LISTINGS", 1):
            self.assertEqual(self.run_at(), ("failed", 0))
            self.connection.refresh_from_db()
            state = self.connection.settings[reminders.KEY]
            self.assertTrue(state["capacity_exhausted"])
            self.assertEqual(state["listings"], {"101": original_state})
            self.feed.draft, self.feed.latest_observations = original_draft, original_observations
            self.feed.save(update_fields=["draft", "latest_observations"])
            self.assertEqual(self.run_at(), ("success", 0))
        self.assertEqual(Notification.objects.count(), 1)

    def test_existing_worker_wires_reminders_and_reports_failure_without_payload(self):
        zero = {"success": 0, "failed": 0, "skipped": 0}
        for fails in (False, True):
            output = io.StringIO()
            with patch.object(reminders, "scan_saved", side_effect=RuntimeError("PRIVATE") if fails else None,
                    return_value=("success", 2)) as scan, \
                    patch("pool_service.avito_tariff_reminder.scan_due", return_value=zero), \
                    patch("pool_service.avito_advance.scan_due", return_value=zero), \
                    patch("pool_service.avito_autoload_auto.scan_due", return_value=zero), \
                    patch("pool_service.avito_daily_statistics.scan_due", return_value=zero):
                if fails:
                    with self.assertRaises(CommandError) as raised:
                        call_command("monitor_avito_statuses", stdout=output)
                    self.assertEqual(raised.exception.returncode, 2)
                else:
                    call_command("monitor_avito_statuses", stdout=output)
                scan.assert_called_once()
            self.assertIn("AVITO_PLACEMENT_REMINDER", output.getvalue())
            self.assertNotIn("PRIVATE", output.getvalue())

    def begin_runtime(self, now=NOW):
        import uuid
        from pool_service.communication_models import AvitoSchedulerHeartbeat
        self.run_id = str(uuid.uuid4())
        self.heartbeat, _ = AvitoSchedulerHeartbeat.objects.update_or_create(
            key="avito-status-monitor", defaults={"run_id": self.run_id, "state": "running",
                "started_at": now - timedelta(seconds=1), "finished_at": None, "checks": []})

    def finish_runtime(self, now=NOW):
        self.heartbeat.refresh_from_db()
        self.heartbeat.state, self.heartbeat.finished_at = "finished", now
        self.heartbeat.save()

    def runtime_row(self):
        from pool_service import avito_runtime as runtime
        self.connection.refresh_from_db()
        return runtime.checks(self.connection, only_check="placement_reminder")[0]

    def record_observer(self, *args, **kwargs):
        from pool_service import avito_runtime as runtime
        runtime.record(*args, run_id=self.run_id, **kwargs)

    def test_night_completion_is_visible_without_refresh_or_stage_and_morning_creates(self):
        night = NOW.replace(hour=0)
        self.begin_runtime(night)
        with patch.object(reminders.timezone, "now", return_value=night), \
                patch("pool_service.avito_services_feed.verify_owned_observations") as verify:
            self.assertEqual(reminders.scan_saved(deadline=reminders.time.monotonic() + 90,
                observer=self.record_observer), ("skipped", 0))
            self.assertFalse(self.runtime_row()["last_worker_verified"])
            self.finish_runtime(night)
            row = self.runtime_row()
            self.assertEqual(row["last_worker_code"], "quiet_hours")
            self.assertEqual(row["last_worker_notification_count"], 0)
            self.assertTrue(row["last_worker_fresh"])
            self.assertFalse(row["window_open"])
        verify.assert_not_called()
        self.connection.refresh_from_db()
        self.assertNotIn(reminders.KEY, self.connection.settings)
        self.assertFalse(Notification.objects.exists())
        self.begin_runtime()
        self.assertEqual(reminders.scan_saved(observer=self.record_observer), ("success", 1))
        self.finish_runtime()
        row = self.runtime_row()
        self.assertEqual(row["last_worker_notification_count"], 1)
        self.assertEqual(row["last_worker_outcome"], "success")
        self.assertIsNone(row["checked_at"])  # Never attribute legacy settings to this run.

    def test_fixed_skip_reasons_subscription_rights_stale_and_invalid(self):
        cases = (("missing", "not_configured"), ("disabled", "disabled"),
                 ("rights", "placement_recipient_unavailable"), ("stale", "placement_stale"),
                 ("invalid", "placement_data_invalid"))
        for case, expected in cases:
            with self.subTest(case=case):
                self.begin_runtime()
                self.feed.draft = [{"rows": [{"Id": "synthetic", "AvitoId": "101"}]}]
                self.observe("2026-01-09")
                if case == "missing":
                    self.monitor.delete()
                elif case == "disabled":
                    self.monitor.enabled = False
                    self.monitor.save()
                elif case == "rights":
                    CommunicationAccess.objects.filter(user=self.owner).update(can_manage_channels=False)
                elif case == "stale":
                    self.observe("2026-01-09", checked=NOW - timedelta(hours=25))
                else:
                    self.feed.draft = []
                    self.feed.save()
                self.assertEqual(reminders.scan_saved(observer=self.record_observer), ("skipped", 0))
                self.finish_runtime()
                row = self.runtime_row()
                self.assertEqual(row["last_worker_code"], expected)
                self.assertEqual(row["last_worker_notification_count"], 0)
                if case == "missing":
                    self.monitor.pk = None
                self.monitor.enabled = True
                self.monitor.save()
                CommunicationAccess.objects.filter(user=self.owner).update(can_manage_channels=True)

    def test_refresh_failure_and_capacity_keep_real_transaction_count(self):
        for age, expected in ((13, 1), (25, 0)):
            self.observe("2026-01-09", checked=NOW - timedelta(hours=age))
            self.begin_runtime()
            with patch("pool_service.avito_services_feed.verify_owned_observations", side_effect=AvitoError("PRIVATE")):
                self.assertEqual(reminders.scan_saved(deadline=reminders.time.monotonic() + 90,
                    observer=self.record_observer), ("failed", expected))
            self.finish_runtime()
            row = self.runtime_row()
            self.assertEqual(row["last_worker_code"], "placement_refresh_failed")
            self.assertEqual(row["last_worker_notification_count"], expected)
            self.assertNotIn("PRIVATE", str(row))
        self.observe("2026-01-09")
        self.begin_runtime()
        with patch.object(reminders, "MAX_TRACKED_LISTINGS", 0):
            self.assertEqual(reminders.scan_saved(observer=self.record_observer), ("failed", 0))
        self.finish_runtime()
        self.assertEqual(self.runtime_row()["last_worker_code"], "placement_capacity_exhausted")

    def test_unknown_stale_run_id_and_binding_cannot_become_current_count(self):
        from pool_service import avito_runtime as runtime
        self.assertIsNone(self.runtime_row()["last_worker_notification_count"])
        self.begin_runtime()
        runtime.record(4, "placement_reminder", "success", run_id="00000000-0000-0000-0000-000000000001", notification_count=1)
        runtime.record(4, "placement_reminder", "success", run_id=self.run_id, notification_count=1,
            binding={"account_id": "wrong"})
        self.finish_runtime()
        self.assertFalse(self.runtime_row()["last_worker_verified"])
        self.begin_runtime()
        reminders.scan_saved(observer=self.record_observer)
        self.finish_runtime()
        with patch.object(reminders.timezone, "now", return_value=NOW + timedelta(hours=2)):
            row = self.runtime_row()
            self.assertTrue(row["last_worker_verified"])
            self.assertFalse(row["last_worker_fresh"])
            self.assertEqual(row["last_worker_checked_at"], NOW.isoformat())
        # Runtime only accepts the current account/org/channel binding.
        ChannelConnection.objects.filter(pk=4).update(external_id="other-account")
        self.connection.refresh_from_db()
        self.assertEqual(runtime.checks(self.connection, only_check="placement_reminder"), [])
        ChannelConnection.objects.filter(pk=4).update(external_id="446312402")
        other = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Other")
        ChannelConnection.objects.filter(pk=4).update(channel=other)
        self.assertFalse(self.runtime_row()["last_worker_verified"])
        ChannelConnection.objects.filter(pk=4).update(channel=self.channel)
        foreign = Organization.objects.create(name="Foreign binding")
        CommunicationChannel.objects.filter(pk=self.channel.pk).update(organization=foreign)
        self.assertFalse(self.runtime_row()["last_worker_verified"])
        goods = ChannelConnection.objects.create(pk=1, channel=self.channel, external_id="123", settings={"keep": 1})
        self.begin_runtime()
        runtime.record(1, "placement_reminder", "success", run_id=self.run_id, notification_count=1)
        self.finish_runtime()
        goods.refresh_from_db()
        self.assertEqual(runtime.checks(goods, only_check="placement_reminder"), [])
        self.assertEqual(goods.settings, {"keep": 1})

    def test_runtime_count_and_receipt_schema_are_bounded_and_check_specific(self):
        from pool_service import avito_runtime as runtime
        from scripts.avito_readonly_receipt import runtime_checks
        self.begin_runtime()
        for count in (True, -1, 201, "PRIVATE", None):
            runtime.record(4, "placement_reminder", "failed", run_id=self.run_id,
                reason="placement_refresh_failed", notification_count=count)
            self.finish_runtime()
            row = self.runtime_row()
            self.assertIsNone(row["last_worker_notification_count"])
            self.assertEqual(runtime_checks([row])[0]["last_worker_code"], "placement_refresh_failed")
            self.heartbeat.state, self.heartbeat.finished_at = "running", None
            self.heartbeat.save()
        self.assertEqual(runtime.safe_code("placement_refresh_failed", check="advance"), "check_error_unclassified")
        self.finish_runtime()
        row = self.runtime_row()
        for changes in ({"source": "PRIVATE"}, {"last_worker_notification_count": True},
                        {"last_worker_notification_count": 201}, {"last_worker_code": "placement_PRIVATE"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                runtime_checks([dict(row, **changes)])
        row["private_payload"] = "PRIVATE"
        self.assertNotIn("PRIVATE", str(runtime_checks([row])))

    def test_worker_uses_active_supervisor_and_exception_count_remains_unknown(self):
        import os
        self.begin_runtime()
        zero = {"success": 0, "failed": 0, "skipped": 0}
        with patch.dict(os.environ, {"SERVICE2_AVITO_CRON_SUPERVISED": "1"}), \
                patch("pool_service.avito_tariff_reminder.scan_due", return_value=zero), \
                patch("pool_service.avito_advance.scan_due", return_value=zero), \
                patch("pool_service.avito_autoload_auto.scan_due", return_value=zero), \
                patch("pool_service.avito_daily_statistics.scan_due", return_value=zero):
            call_command("monitor_avito_statuses", stdout=io.StringIO())
            self.finish_runtime()
            self.assertEqual(self.runtime_row()["last_worker_notification_count"], 1)
            self.begin_runtime()
            with patch.object(reminders, "_scan_saved", side_effect=RuntimeError("PRIVATE")):
                with self.assertRaises(CommandError):
                    call_command("monitor_avito_statuses", stdout=io.StringIO())
            self.finish_runtime()
            row = self.runtime_row()
            self.assertEqual(row["last_worker_code"], "worker_check_exception")
            self.assertIsNone(row["last_worker_notification_count"])

    def test_existing_diagnostics_ui_permissions_and_mcp_projection_are_read_only(self):
        from django.core.exceptions import PermissionDenied
        from django.urls import reverse
        from pool_service.avito_mcp_reader import read_tool
        from pool_service.communication_models import AvitoCredential
        AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted="synthetic-id", client_secret_encrypted="synthetic-secret")
        self.begin_runtime()
        reminders.scan_saved(observer=self.record_observer)
        self.finish_runtime()
        url = reverse("communication_connection_edit", args=[4])
        self.client.force_login(self.owner)
        with patch("pool_service.avito_management.access_token") as token:
            response = self.client.get(url)
            self.assertContains(response, "Напоминания о сроках размещения Услуг")
            self.assertContains(response, "Создано in-app записей: 1")
            self.assertContains(response, "Доставка и прочтение не подтверждены")
            self.assertContains(response, "saved_owned_avito_item_detail")
            data = read_tool(self.owner, "get_avito_diagnostics", {"connection_id": 4})
        token.assert_not_called()
        row = next(x for x in data["data"]["components"] if x["component"] == "placement_reminder")
        self.assertEqual(row["last_worker_notification_count"], 1)
        self.assertTrue(row["last_worker_verified"])
        self.assertFalse(data["provider_request_performed"])
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(url).status_code, 403)
        with self.assertRaises(PermissionDenied):
            read_tool(self.manager, "get_avito_diagnostics", {"connection_id": 4})
        foreign = User.objects.create_user("synthetic-foreign-owner")
        org = Organization.objects.create(name="Foreign synthetic")
        OrganizationAccess.objects.create(user=foreign, organization=org, role="owner")
        self.client.force_login(foreign)
        self.assertEqual(self.client.get(url).status_code, 404)
        with self.assertRaises(PermissionDenied):
            read_tool(foreign, "get_avito_diagnostics", {"connection_id": 4})

    def test_channel_move_during_real_refresh_cannot_adopt_fallback_or_evidence(self):
        self.observe("2026-01-09", checked=NOW - timedelta(hours=13))
        original_checked = self.feed.last_checked_at
        other = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Moved channel")
        refreshed = deepcopy(self.feed.latest_observations)
        refreshed[0]["checked_at"] = NOW.isoformat()
        self.begin_runtime()

        def move_during_read(*args, **kwargs):
            ChannelConnection.objects.filter(pk=4).update(channel=other)
            return {"observations": refreshed}

        with patch("pool_service.avito_services_feed.verify_inventory", side_effect=move_during_read) as reader, \
                patch.object(self, "record_observer", wraps=self.record_observer) as observer:
            self.assertEqual(reminders.scan_saved(deadline=reminders.time.monotonic() + 90,
                observer=observer), ("failed", 0))
        reader.assert_called_once()
        observer.assert_not_called()
        self.finish_runtime()
        self.assertFalse(Notification.objects.exists())
        self.connection.refresh_from_db()
        self.assertNotIn(reminders.KEY, self.connection.settings)
        self.feed.refresh_from_db()
        self.assertEqual(self.feed.last_checked_at, original_checked)
        self.assertFalse(self.feed.versions.filter(action="verify").exists())
        row = self.runtime_row()
        self.assertFalse(row["last_worker_verified"])
        self.assertIsNone(row["last_worker_notification_count"])

    def test_channel_move_before_refresh_prevents_provider_read_and_local_fallback(self):
        self.observe("2026-01-09", checked=NOW - timedelta(hours=13))
        original = reminders.capture_binding()
        other = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Moved before read")

        def capture_then_move():
            ChannelConnection.objects.filter(pk=4).update(channel=other)
            return original

        with patch.object(reminders, "capture_binding", side_effect=capture_then_move), \
                patch("pool_service.avito_services_feed.verify_inventory") as reader, \
                patch.object(self, "record_observer", wraps=self.record_observer) as observer:
            self.assertEqual(reminders.scan_saved(deadline=reminders.time.monotonic() + 90,
                observer=observer), ("skipped", 0))
        reader.assert_not_called()
        observer.assert_not_called()
        self.assertFalse(Notification.objects.exists())
        self.connection.refresh_from_db()
        self.assertNotIn(reminders.KEY, self.connection.settings)

    def test_command_exception_after_channel_move_does_not_attribute_failure_to_new_channel(self):
        import os
        original = reminders.capture_binding()
        other = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Exception drift")
        self.begin_runtime()
        zero = {"success": 0, "failed": 0, "skipped": 0}

        def move_then_fail(**kwargs):
            self.assertEqual(kwargs["expected_binding"], original)
            ChannelConnection.objects.filter(pk=4).update(channel=other)
            raise RuntimeError("PRIVATE")

        with patch.dict(os.environ, {"SERVICE2_AVITO_CRON_SUPERVISED": "1"}), \
                patch.object(reminders, "scan_saved", side_effect=move_then_fail), \
                patch("pool_service.avito_tariff_reminder.scan_due", return_value=zero), \
                patch("pool_service.avito_advance.scan_due", return_value=zero), \
                patch("pool_service.avito_autoload_auto.scan_due", return_value=zero), \
                patch("pool_service.avito_daily_statistics.scan_due", return_value=zero):
            with self.assertRaises(CommandError):
                call_command("monitor_avito_statuses", stdout=io.StringIO())
        self.finish_runtime()
        self.assertFalse(Notification.objects.exists())
        row = self.runtime_row()
        self.assertFalse(row["last_worker_verified"])
        self.assertIsNone(row["last_worker_notification_count"])
        self.assertIsNone(row["last_worker_code"])
        self.assertFalse(any(entry["check"] == "placement_reminder" and entry["channel_id"] == other.pk
                             for entry in self.heartbeat.checks))
