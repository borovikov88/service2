from datetime import date, datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
import io

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.test import TestCase, Client, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_tariff_reminder as calendar, avito_advance, avito_readonly_audit
from pool_service.communication_models import AvitoCredential
from pool_service.models import Notification, OrganizationAccess, Profile
from pool_service.tests.test_avito_management import AvitoManagementTests
from scripts import avito_readonly_receipt as receipt

REAL_TODAY = calendar.today


class TariffReminderTests(TestCase):
    def setUp(self):
        AvitoManagementTests.setUp(self)
        for target, result in (("scheduler_state", {"ready": True, "worker_ready": True, "detail": "Synthetic tick"}),
                               ("today", date(2026, 1, 15))):
            mocked = patch.object(calendar, target, return_value=result)
            mocked.start()
            self.addCleanup(mocked.stop)

    def enable(self, day=date(2026, 1, 18)):
        return calendar.configure(self.connection, self.owner, "enable", expires_on=day)

    def poll(self, day):
        self.connection.refresh_from_db()
        data = dict(self.connection.settings)
        state = dict(data[calendar.KEY])
        state["next_due_at"] = (timezone.now() - timedelta(seconds=1)).isoformat()
        data[calendar.KEY] = state
        self.connection.settings = data
        self.connection.save(update_fields=["settings"])
        with patch.object(calendar, "today", return_value=day):
            return calendar.scan_connection(self.connection.pk)

    def state(self):
        self.connection.refresh_from_db()
        return self.connection.settings[calendar.KEY]

    def test_stages_repeats_and_first_activation_inside_or_after_window(self):
        self.enable(date(2026, 1, 19))
        for day, count in ((15, 0), (16, 1), (17, 1), (18, 2), (19, 3), (20, 3)):
            self.assertEqual(self.poll(date(2026, 1, day)), "success")
            self.assertEqual(Notification.objects.count(), count)
        self.assertEqual(set(Notification.objects.values_list("user_id", flat=True)), {self.owner.pk})
        self.assertEqual(set(Notification.objects.values_list("organization_id", flat=True)), {self.org.pk})
        self.assertTrue(all("вручную" in row.message for row in Notification.objects.all()))
        calendar.configure(self.connection, self.owner, "disable")
        self.enable(date(2026, 1, 14))
        self.assertEqual(self.poll(date(2026, 1, 15)), "success")
        self.assertEqual(Notification.objects.count(), 4)

    def test_date_change_rearms_but_same_date_enable_and_repeat_scan_do_not_duplicate(self):
        first = self.enable()
        self.poll(date(2026, 1, 15))
        self.assertEqual(self.enable()["generation"], first["generation"])
        self.assertEqual(calendar.scan_connection(self.connection.pk), "skipped")
        changed = self.enable(date(2026, 1, 20))
        self.assertNotEqual(changed["generation"], first["generation"])
        self.poll(date(2026, 1, 15))
        self.assertEqual(Notification.objects.count(), 1)
        self.poll(date(2026, 1, 17))
        self.assertEqual(Notification.objects.count(), 2)

    def test_manual_date_has_no_api_dependency_or_credential_binding(self):
        self.enable()
        AvitoCredential.objects.filter(connection=self.connection).delete()
        with patch("pool_service.communication_avito.access_token", side_effect=AssertionError("No API")):
            self.assertEqual(self.poll(date(2026, 1, 15)), "success")
        self.assertEqual(Notification.objects.count(), 1)

    def test_preferences_do_not_consume_stage_and_push_is_not_sent(self):
        self.enable()
        Profile.objects.update_or_create(user=self.owner, defaults={"in_app_notifications_enabled": False})
        self.assertEqual(self.poll(date(2026, 1, 15)), "failed")
        self.assertNotIn("last_stage", self.state())
        self.assertFalse(Notification.objects.exists())
        self.assertTrue(self.state()["last_error_detail"])
        Profile.objects.filter(user=self.owner).update(in_app_notifications_enabled=True)
        with patch("pool_service.services.notifications.send_push_to_users") as push:
            self.assertEqual(self.poll(date(2026, 1, 15)), "success")
        push.assert_not_called()
        self.assertEqual(Notification.objects.count(), 1)

    def test_revoked_actor_and_account_changes_disable_reminders(self):
        self.enable()
        self.connection.external_id = "999"
        self.connection.save(update_fields=["external_id"])
        self.assertEqual(self.poll(date(2026, 1, 15)), "failed")
        self.assertFalse(self.state()["enabled"])
        self.assertFalse(Notification.objects.exists())
        self.connection.external_id = "123"
        self.connection.save(update_fields=["external_id"])
        self.enable()
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.assertEqual(self.poll(date(2026, 1, 15)), "failed")
        self.assertFalse(Notification.objects.exists())

    def test_other_admin_and_manager_cannot_take_over_and_invalid_inputs_fail(self):
        self.enable()
        admin = User.objects.create_user("synthetic-tariff-admin")
        OrganizationAccess.objects.create(user=admin, organization=self.org, role="admin")
        self.assertTrue(calendar.subscriber_allowed(admin, self.org))
        for user in (admin, self.manager):
            for action in ("enable", "disable", "retry"):
                with self.assertRaises(PermissionDenied):
                    calendar.configure(self.connection, user, action, expires_on=date(2026, 1, 18))
        for invalid in (None, "2026-01-18", datetime(2026, 1, 18)):
            with self.assertRaises(ValueError):
                calendar.configure(self.connection, self.owner, "enable", expires_on=invalid)
        with patch.object(calendar, "scheduler_state", return_value={"ready": False, "worker_ready": False}):
            with self.assertRaises(ValueError):
                self.enable(date(2026, 1, 19))

    def test_notification_and_stage_rollback_together(self):
        self.enable()
        before = self.state()
        with patch.object(calendar, "notify_users", side_effect=RuntimeError("Synthetic rollback")):
            with self.assertRaises(RuntimeError):
                calendar.scan_connection(self.connection.pk)
        self.assertEqual(self.state(), before)
        self.assertFalse(Notification.objects.exists())
        self.assertEqual(self.poll(date(2026, 1, 15)), "success")
        self.assertEqual(Notification.objects.count(), 1)

    @override_settings(COMMUNICATION_TIME_ZONE="Asia/Barnaul")
    def test_local_date_boundary_and_deadline_budget(self):
        with patch("django.utils.timezone.now", return_value=datetime(2026, 1, 15, 18, tzinfo=dt_timezone.utc)):
            self.assertEqual(REAL_TODAY(), date(2026, 1, 16))
        self.enable()
        with patch("time.monotonic", return_value=100), patch.object(calendar, "scan_connection") as scan:
            calendar.scan_due(deadline=104)
        scan.assert_not_called()

    def test_csrf_self_recipient_and_cache_only_get(self):
        self.client.force_login(self.owner)
        url = reverse("avito_refresh_data", args=[self.connection.pk])
        with patch("pool_service.avito_management.access_token") as token:
            self.client.post(url, {"section": "tariff_enable", "expires_on": "2026-01-18", "actor_id": self.manager.pk})
            self.assertEqual(self.state()["actor_id"], self.owner.pk)
            response = self.client.get(reverse("avito_dashboard"))
        token.assert_not_called()
        self.assertContains(response, "дата указана вручную")
        self.assertContains(response, "Пуш, SMS и письма не отправляются")
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(url, {"section": "tariff_disable"}).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(url, {"section": "tariff_disable"}).status_code, 403)

    def test_readonly_monitor_flags_are_scoped_and_projection_drops_private_fields(self):
        with patch.object(avito_advance, "scheduler_state", return_value={"ready": True}):
            state = avito_advance.configure(self.connection, self.owner, "enable")
        self.enable()
        prefix = f"avito-advance:{self.connection.pk}:{state['generation']}:"
        Notification.objects.create(user=self.manager, organization=self.org,
            title="Synthetic other recipient", message="Synthetic", kind="communication", dedupe_key=prefix + "other")
        flags = avito_readonly_audit.monitoring_state(self.connection)
        self.assertTrue(flags["advance_enabled"])
        self.assertTrue(flags["advance_scope_valid"])
        self.assertFalse(flags["advance_notification_exists"])
        Notification.objects.create(user=self.owner, organization=self.org,
            title="Synthetic notice", message="Synthetic", kind="communication", dedupe_key=prefix + "self")
        flags = avito_readonly_audit.monitoring_state(self.connection)
        self.assertTrue(flags["advance_notification_exists"])
        projected = receipt.monitoring(dict(flags, amount="PRIVATE", actor_id=123, expires_on="PRIVATE"))
        self.assertNotIn("PRIVATE", str(projected))
        self.assertNotIn("actor_id", projected)
        for changed in ({"amount_logged": True}, {"recipient_logged": True}, {"advance_enabled": 1}):
            with self.assertRaises(ValueError):
                receipt.monitoring(dict(flags, **changed))
        self.assertEqual(self.state()["source"], "manual")

    def test_calendar_runs_before_api_checks_and_survives_provider_failure(self):
        self.enable()
        calls = []
        def local(**kwargs):
            calls.append("calendar")
            return {"success": 1, "failed": 0, "skipped": 0}
        with patch.object(calendar, "scan_due", side_effect=local), \
             patch("pool_service.avito_advance.scan_due", side_effect=lambda **kwargs: calls.append("advance") or {"success": 0, "failed": 0, "skipped": 0}), \
             patch("pool_service.avito_autoload_auto.scan_due", side_effect=lambda **kwargs: calls.append("report") or {"success": 0, "failed": 0, "skipped": 0}):
            call_command("monitor_avito_statuses", stdout=io.StringIO())
        self.assertEqual(calls, ["calendar", "advance", "report"])


class ServicesTariffReminderTests(TestCase):
    """Services-only additions; legacy goods reminders retain their contract."""
    enable = TariffReminderTests.enable
    state = TariffReminderTests.state

    def setUp(self):
        from pool_service.communication_models import ChannelConnection
        TariffReminderTests.setUp(self)
        if self.connection.pk == 4:
            self.connection.delete()
            self.connection = ChannelConnection.objects.create(pk=1, channel=self.channel,
                name="Goods fixture", external_id="123")
        self.goods = self.connection
        self.connection = ChannelConnection.objects.create(pk=4, channel=self.channel,
            name="Services fixture", external_id="446312402")
        clock = patch.object(calendar.timezone, "now",
            return_value=datetime(2026, 1, 15, 5, tzinfo=dt_timezone.utc))
        clock.start()
        self.addCleanup(clock.stop)

    def poll(self, day):
        now = datetime(day.year, day.month, day.day, 5, tzinfo=dt_timezone.utc)
        with patch.object(calendar.timezone, "now", return_value=now):
            return TariffReminderTests.poll(self, day)

    def test_missing_date_does_not_invent_confirmation_or_call_provider(self):
        with patch("pool_service.communication_avito.access_token") as token:
            state = calendar.display_state(self.connection, self.owner)
        token.assert_not_called()
        self.assertIsNone(state["expires_on"])
        self.assertEqual(state["confirmed_at_display"], "")
        self.assertEqual(state["confirmation_source_label"], "")
        self.assertTrue(state["services_schedule"])
        self.connection.refresh_from_db()
        self.assertNotIn(calendar.KEY, self.connection.settings)

    def test_explicit_source_time_and_same_date_save_do_not_rearm_notifications(self):
        first = self.enable(date(2026, 1, 22))
        self.assertEqual(first["source"], "manual")
        self.assertEqual(first["confirmation_source"], calendar.MANUAL_SOURCE)
        self.assertEqual(first["confirmed_by"], self.owner.pk)
        self.assertEqual(first["confirmed_at"], "2026-01-15T05:00:00+00:00")
        self.poll(date(2026, 1, 15))
        self.assertEqual(Notification.objects.count(), 1)
        with patch.object(calendar.timezone, "now",
                return_value=datetime(2026, 1, 15, 6, tzinfo=dt_timezone.utc)):
            second = self.enable(date(2026, 1, 22))
            before = second["confirmed_at"]
            calendar.configure(self.connection, self.owner, "retry")
        self.assertEqual(second["generation"], first["generation"])
        self.assertEqual(second["confirmed_at"], "2026-01-15T06:00:00+00:00")
        self.assertEqual(self.state()["confirmed_at"], before)
        self.poll(date(2026, 1, 15))
        self.assertEqual(Notification.objects.count(), 1)
        calendar.configure(self.connection, self.owner, "disable")
        self.assertEqual(self.state()["confirmed_at"], before)

    def test_7_3_1_and_due_stages_are_distinct_deduplicated_and_do_not_burst(self):
        self.enable(date(2026, 1, 22))
        for day, count in ((14, 0), (15, 1), (16, 1), (19, 2), (20, 2),
                           (21, 3), (22, 4), (23, 4)):
            self.assertEqual(self.poll(date(2026, 1, day)), "success")
            self.assertEqual(Notification.objects.count(), count)
        self.assertEqual(set(row.dedupe_key.rsplit(":", 1)[1]
                             for row in Notification.objects.all()),
                         {"week", "soon", "tomorrow", "due"})
        self.assertTrue(all("вручную" in row.message for row in Notification.objects.all()))
        changed = self.enable(date(2026, 1, 14))
        self.assertEqual(self.poll(date(2026, 1, 15)), "success")
        self.assertEqual(Notification.objects.count(), 5)
        self.assertEqual(self.state()["last_stage"], "due")

    def test_services_reminder_quiet_hours_and_retries_defer_without_consuming_stage(self):
        with patch.object(calendar.timezone, "now",
                return_value=datetime(2026, 1, 15, 16, tzinfo=dt_timezone.utc)):
            self.enable(date(2026, 1, 22))
            self.assertEqual(TariffReminderTests.poll(self, date(2026, 1, 15)), "skipped")
            self.assertFalse(Notification.objects.exists())
            self.assertNotIn("last_stage", self.state())
            self.assertEqual(calendar._time(self.state()["next_due_at"]).astimezone(
                calendar.SERVICES_ZONE).strftime("%Y-%m-%d %H:%M"), "2026-01-16 08:00")
            calendar.configure(self.connection, self.owner, "retry")
            self.assertEqual(TariffReminderTests.poll(self, date(2026, 1, 15)), "skipped")
            self.assertFalse(Notification.objects.exists())
        with patch.object(calendar.timezone, "now",
                return_value=datetime(2026, 1, 16, 1, tzinfo=dt_timezone.utc)):
            self.assertEqual(TariffReminderTests.poll(self, date(2026, 1, 16)), "success")
            self.assertEqual(Notification.objects.count(), 1)
            self.assertEqual(self.state()["last_stage"], "week")
            self.assertNotIn("quiet_until_at", self.state())

    def test_old_manual_dates_have_no_invented_confirmation_time_or_source(self):
        self.enable(date(2026, 1, 22))
        self.connection.refresh_from_db()
        data = dict(self.connection.settings)
        state = dict(data[calendar.KEY])
        for key in ("confirmed_at", "confirmed_by", "confirmation_source"):
            state.pop(key)
        data[calendar.KEY] = state
        self.connection.settings = data
        self.connection.save(update_fields=["settings"])
        shown = calendar.display_state(self.connection, self.owner)
        self.assertEqual(shown["confirmed_at_display"], "")
        self.assertEqual(shown["confirmation_source_label"], "")
        self.assertEqual(shown["expires_on"], date(2026, 1, 22))
        self.assertEqual(self.poll(date(2026, 1, 15)), "success")

    def test_foreign_binding_cannot_inherit_a_new_confirmation(self):
        self.enable(date(2026, 1, 22))
        self.connection.external_id = "foreign-account"
        self.connection.save(update_fields=["external_id"])
        shown = calendar.display_state(self.connection, self.owner)
        self.assertEqual(shown["confirmed_at_display"], "")
        self.assertEqual(shown["confirmation_source_label"], "")
        self.assertFalse(shown["services_schedule"])
        self.assertEqual(self.poll(date(2026, 1, 15)), "failed")
        self.assertFalse(Notification.objects.exists())

    def test_goods_schedule_and_settings_do_not_inherit_services_additions(self):
        before = self.goods.settings
        calendar.configure(self.goods, self.owner, "enable", expires_on=date(2026, 1, 22))
        self.goods.refresh_from_db()
        goods = dict(self.goods.settings[calendar.KEY])
        self.assertNotIn("confirmed_at", goods)
        self.assertNotIn("confirmation_source", goods)
        self.assertEqual(calendar._stage(date(2026, 1, 22), date(2026, 1, 15)), "")
        self.enable(date(2026, 1, 22))
        self.poll(date(2026, 1, 15))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings[calendar.KEY], goods)

    @override_settings(COMMUNICATION_TIME_ZONE="Europe/Moscow")
    def test_services_date_and_display_are_fixed_to_barnaul(self):
        with patch.object(calendar.timezone, "now",
                return_value=datetime(2026, 1, 15, 18, tzinfo=dt_timezone.utc)):
            self.assertEqual(calendar._today_for(self.connection), date(2026, 1, 16))
            self.enable(date(2026, 1, 23))
            self.connection.refresh_from_db()
            shown = calendar.display_state(self.connection, self.owner)
        self.assertEqual(shown["timezone"], "Asia/Barnaul")
        self.assertEqual(shown["confirmed_at_display"], "16.01.2026 01:00:00")


    def test_save_date_works_without_worker_but_never_enables_opted_out_reminders(self):
        with patch.object(calendar, "scheduler_state", return_value={"ready": False, "worker_ready": False}):
            saved = calendar.configure(self.connection, self.owner, "save_date", expires_on=date(2026, 1, 22))
            self.assertFalse(saved["enabled"])
            self.assertIsNone(saved["next_due_at"])
            self.assertEqual(saved["confirmation_source"], calendar.MANUAL_SOURCE)
            self.assertEqual(saved["confirmed_by"], self.owner.pk)
            self.assertEqual(self.poll(date(2026, 1, 15)), "skipped")
            self.assertFalse(Notification.objects.exists())
            with self.assertRaises(ValueError):
                self.enable(date(2026, 1, 22))
        self.enable(date(2026, 1, 22))
        self.poll(date(2026, 1, 15))
        self.assertEqual(Notification.objects.count(), 1)
        calendar.configure(self.connection, self.owner, "disable")
        with patch.object(calendar, "scheduler_state", return_value={"ready": False, "worker_ready": False}):
            saved = calendar.configure(self.connection, self.owner, "save_date", expires_on=date(2026, 1, 23))
        self.assertFalse(saved["enabled"])
        self.assertIsNone(saved["next_due_at"])
        self.assertNotIn("last_stage", saved)

    def test_date_only_save_preserves_enabled_state_and_resets_only_changed_date_dedupe(self):
        first = self.enable(date(2026, 1, 22))
        self.poll(date(2026, 1, 15))
        with patch.object(calendar, "scheduler_state", return_value={"ready": False, "worker_ready": False}):
            same = calendar.configure(self.connection, self.owner, "save_date", expires_on=date(2026, 1, 22))
            self.assertTrue(same["enabled"])
            self.assertEqual(same["generation"], first["generation"])
            self.assertEqual(same["last_stage"], "week")
            changed = calendar.configure(self.connection, self.owner, "save_date", expires_on=date(2026, 1, 23))
        self.assertTrue(changed["enabled"])
        self.assertNotEqual(changed["generation"], first["generation"])
        self.assertNotIn("last_stage", changed)
        self.assertEqual(self.poll(date(2026, 1, 16)), "success")
        self.assertEqual(Notification.objects.count(), 2)

    def test_date_only_form_is_self_scoped_csrf_protected_and_unavailable_for_goods(self):
        url = reverse("avito_refresh_data", args=[4])
        self.client.force_login(self.owner)
        with patch.object(calendar, "scheduler_state", return_value={"ready": False, "worker_ready": False}), \
             patch("pool_service.communication_avito.access_token") as token:
            response = self.client.post(url, {"section": "tariff_save_date", "expires_on": "2026-01-22",
                                             "actor_id": self.manager.pk})
        self.assertEqual(response.status_code, 302)
        token.assert_not_called()
        self.assertEqual(self.state()["actor_id"], self.owner.pk)
        self.assertFalse(self.state()["enabled"])
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.owner)
        self.assertEqual(client.post(url, {"section": "tariff_save_date",
                         "expires_on": "2026-01-22"}).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(url, {"section": "tariff_save_date",
                         "expires_on": "2026-01-22"}).status_code, 403)
        self.goods.refresh_from_db()
        before = dict(self.goods.settings)
        with self.assertRaises(ValueError):
            calendar.configure(self.goods, self.owner, "save_date", expires_on=date(2026, 1, 22))
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, before)
