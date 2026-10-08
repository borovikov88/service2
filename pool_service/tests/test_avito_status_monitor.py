import io
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.management import call_command, CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase, Client
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_status_monitor as monitor
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import (
    AvitoApiThrottle, AvitoListingStatus, AvitoStatusMonitor, ChannelConnection, CommunicationChannel,
)
from pool_service.models import Notification, Organization, OrganizationAccess


def listing(identifier=1, status="active"):
    return {"id": str(identifier), "status": status, "title": f"Listing {identifier}"}


class AvitoMonitorFixture:
    def setUp(self):
        self.org = Organization.objects.create(name="Monitor test")
        self.owner = User.objects.create_user("monitor-owner", password="test")
        self.admin = User.objects.create_user("monitor-admin", password="test")
        self.manager = User.objects.create_user("monitor-manager", password="test")
        for user, role in ((self.owner, "owner"), (self.admin, "admin"), (self.manager, "manager")):
            OrganizationAccess.objects.create(organization=self.org, user=user, role=role)
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Avito")
        self.connection = ChannelConnection.objects.create(channel=self.channel, external_id="123", name="Goods")
        self.url = reverse("avito_configure_monitor", args=[self.connection.pk])
        self.state = monitor.configure(self.connection, self.owner, "enable")

    def due(self):
        AvitoStatusMonitor.objects.filter(pk=self.state.pk).update(next_due_at=timezone.now() - timedelta(seconds=1))

    def run_scan(self, rows):
        self.due()
        with patch.object(monitor, "full_scan", return_value=({x["id"]: x for x in rows}, 1)):
            return monitor.scan_monitor(self.state.pk)


class AvitoMonitorTests(AvitoMonitorFixture, TestCase):
    def test_initial_baseline_is_silent_and_all_bad_transitions_notify_owner_only(self):
        self.assertEqual(self.run_scan([listing(1, "blocked"), listing(2)]), ("baseline", 0))
        self.assertFalse(Notification.objects.exists())
        for state in ("removed", "old", "blocked", "rejected"):
            result = self.run_scan([listing(1, "blocked"), listing(2, state)])
            self.assertEqual(result, ("success", 1))
        self.assertEqual(Notification.objects.count(), 4)
        self.assertEqual(set(Notification.objects.values_list("user_id", flat=True)), {self.owner.pk})
        self.assertTrue(all(x.organization_id == self.org.pk for x in Notification.objects.all()))
        self.state.refresh_from_db()
        self.assertIsNotNone(self.state.baseline_at)
        self.assertGreater(self.state.next_due_at, timezone.now() + timedelta(minutes=59))

    def test_repeated_scans_deduplicate_but_later_recurrence_is_new_event(self):
        self.run_scan([listing()])
        self.run_scan([listing(status="blocked")])
        self.run_scan([listing(status="blocked")])
        self.run_scan([listing()])
        self.run_scan([listing(status="blocked")])
        self.assertEqual(Notification.objects.count(), 2)
        self.assertEqual(Notification.objects.values("dedupe_key").distinct().count(), 2)

    def test_no_push_and_new_unknown_or_missing_items_are_not_status_events(self):
        with patch("pool_service.services.notifications.send_push_to_users") as push:
            self.run_scan([listing()])
            self.run_scan([listing(2, "rejected")])
            self.assertFalse(Notification.objects.exists())
            self.assertEqual(AvitoListingStatus.objects.get(item_id="1").status, "active")
            self.run_scan([listing(1, "old")])
        self.assertEqual(Notification.objects.count(), 1)
        push.assert_not_called()

    def test_failed_scan_preserves_baseline_and_status_and_backoff_is_bounded(self):
        self.run_scan([listing()])
        baseline = AvitoStatusMonitor.objects.get(pk=self.state.pk).baseline_at
        for minutes in (15, 30, 60, 60):
            self.due()
            with patch.object(monitor, "full_scan", side_effect=AvitoError("provider_http_429")):
                self.assertEqual(monitor.scan_monitor(self.state.pk), ("failed", 0))
            self.state.refresh_from_db()
            self.assertEqual(self.state.baseline_at, baseline)
            self.assertEqual(self.state.last_error_code, "provider_http_429")
            self.assertIsNotNone(self.state.last_failure_at)
            seconds = (self.state.next_due_at - timezone.now()).total_seconds()
            self.assertTrue(minutes * 60 - 3 < seconds <= minutes * 60)
        self.assertEqual(AvitoListingStatus.objects.get(item_id="1").status, "active")
        self.assertFalse(Notification.objects.exists())

    def test_failure_before_baseline_does_not_make_later_initial_scan_send_old_alerts(self):
        with patch.object(monitor, "full_scan", side_effect=AvitoError("provider_http_403")):
            monitor.scan_monitor(self.state.pk)
        self.state.refresh_from_db()
        self.assertIsNone(self.state.baseline_at)
        self.assertEqual(self.run_scan([listing(status="rejected")]), ("baseline", 0))

    def test_unexpected_exception_text_is_never_saved(self):
        with patch.object(monitor, "full_scan", side_effect=RuntimeError("PRIVATE token provider text")):
            monitor.scan_monitor(self.state.pk)
        self.state.refresh_from_db()
        self.assertEqual(self.state.last_error_code, "monitor_error")
        self.assertNotIn("PRIVATE", str(self.state.__dict__))

    def test_active_lease_excludes_second_worker(self):
        def scan(_connection):
            self.assertEqual(monitor.scan_monitor(self.state.pk), ("skipped", 0))
            return {"1": listing()}, 1
        with patch.object(monitor, "full_scan", side_effect=scan) as fetch:
            self.assertEqual(monitor.scan_monitor(self.state.pk), ("baseline", 0))
        self.assertEqual(fetch.call_count, 1)

    def test_replaced_lease_discards_stale_worker_result(self):
        def scan(_connection):
            AvitoStatusMonitor.objects.filter(pk=self.state.pk).update(lease_token=uuid.uuid4())
            return {"1": listing()}, 1
        with patch.object(monitor, "full_scan", side_effect=scan):
            self.assertEqual(monitor.scan_monitor(self.state.pk), ("skipped", 0))
        self.assertFalse(AvitoListingStatus.objects.exists())

    def test_expired_worker_lease_recovers_and_records_interruption(self):
        AvitoStatusMonitor.objects.filter(pk=self.state.pk).update(
            lease_token=uuid.uuid4(), lease_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.run_scan([listing()]), ("baseline", 0))
        self.state.refresh_from_db()
        self.assertIsNotNone(self.state.last_failure_at)
        self.assertEqual(self.state.last_error_code, "")

    def test_disabling_during_scan_invalidates_result_and_reenable_rebaselines(self):
        self.run_scan([listing()])
        self.due()
        def scan(_connection):
            monitor.configure(self.connection, self.owner, "disable")
            return {"1": listing(status="blocked")}, 1
        with patch.object(monitor, "full_scan", side_effect=scan):
            self.assertEqual(monitor.scan_monitor(self.state.pk), ("skipped", 0))
        self.assertFalse(Notification.objects.exists())
        monitor.configure(self.connection, self.owner, "enable")
        self.assertEqual(self.run_scan([listing(status="blocked")]), ("baseline", 0))

    def test_revoked_owner_never_receives_scan_or_notification(self):
        OrganizationAccess.objects.filter(user=self.owner).update(role="manager")
        with patch.object(monitor, "full_scan") as fetch:
            self.assertEqual(monitor.scan_monitor(self.state.pk), ("failed", 0))
        fetch.assert_not_called()
        self.state.refresh_from_db()
        self.assertFalse(self.state.enabled)
        self.assertEqual(self.state.last_error_code, "monitor_owner_unavailable")

    def test_connection_move_or_account_change_discards_in_flight_result(self):
        def scan(_connection):
            ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="456")
            return {"1": listing()}, 1
        with patch.object(monitor, "full_scan", side_effect=scan):
            self.assertEqual(monitor.scan_monitor(self.state.pk), ("failed", 0))
        self.assertFalse(AvitoListingStatus.objects.exists())
        self.state.refresh_from_db()
        self.assertFalse(self.state.enabled)

    def test_role_revocation_during_scan_discards_result(self):
        self.run_scan([listing()])
        self.due()
        def scan(_connection):
            OrganizationAccess.objects.filter(user=self.owner).update(role="admin")
            return {"1": listing(status="old")}, 1
        with patch.object(monitor, "full_scan", side_effect=scan):
            self.assertEqual(monitor.scan_monitor(self.state.pk), ("failed", 0))
        self.assertFalse(Notification.objects.exists())

    def test_notification_and_snapshot_are_atomic(self):
        self.run_scan([listing()])
        self.due()
        with patch.object(monitor, "full_scan", return_value=({"1": listing(status="blocked")}, 1)), \
             patch.object(monitor, "notify_users", side_effect=RuntimeError("notification unavailable")):
            self.assertEqual(monitor.scan_monitor(self.state.pk), ("failed", 0))
        self.assertEqual(AvitoListingStatus.objects.get(item_id="1").status, "active")
        self.assertFalse(Notification.objects.exists())

    def test_duplicate_item_snapshot_has_database_constraint(self):
        self.run_scan([listing()])
        with self.assertRaises(IntegrityError), transaction.atomic():
            AvitoListingStatus.objects.create(monitor=self.state, item_id="1", status="old")

    def test_admin_manager_and_unscoped_superuser_cannot_enable_or_retarget(self):
        for user in (self.admin, self.manager, User.objects.create_superuser("unscoped", "", "test")):
            with self.assertRaises(PermissionDenied):
                monitor.configure(self.connection, user, "enable")
            self.client.force_login(user)
            self.assertEqual(self.client.post(self.url, {"action": "enable"}).status_code, 403)

    def test_owner_post_binds_self_ignores_submitted_recipient_and_requires_csrf(self):
        self.client.force_login(self.owner)
        result = self.client.post(self.url, {"action": "enable", "recipient": self.admin.pk})
        self.assertEqual(result.status_code, 302)
        self.state.refresh_from_db()
        self.assertEqual(self.state.recipient_id, self.owner.pk)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(self.url, {"action": "enable"}).status_code, 403)

    def test_other_org_owner_cannot_modify_or_read_monitor(self):
        org = Organization.objects.create(name="Other")
        other = User.objects.create_user("other-owner")
        OrganizationAccess.objects.create(user=other, organization=org, role="owner")
        self.client.force_login(other)
        self.assertEqual(self.client.post(self.url, {"action": "enable"}).status_code, 404)
        self.assertEqual(monitor.display_state(self.connection, other), {})

    def test_gets_show_owner_state_without_provider_reads(self):
        self.client.force_login(self.owner)
        with patch.object(monitor, "full_scan") as fetch:
            self.assertContains(self.client.get(reverse("communication_connection_edit", args=[self.connection.pk])), "Почасовой контроль объявлений")
            self.assertContains(self.client.get(reverse("avito_dashboard")), "Контроль статусов")
        fetch.assert_not_called()
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse("communication_connection_edit", args=[self.connection.pk])), "Почасовой контроль объявлений")

    def test_command_is_due_only_independent_per_connection_and_sanitized(self):
        second = ChannelConnection.objects.create(channel=self.channel, external_id="456", name="Services")
        monitor.configure(second, self.owner, "enable")
        out = io.StringIO()
        with patch.object(monitor, "full_scan", side_effect=[AvitoError("provider_http_403"), ({"2": listing(2)}, 1)]):
            with self.assertRaises(CommandError):
                call_command("monitor_avito_statuses", stdout=out)
        self.assertIn("baseline=1 success=0 failed=1", out.getvalue())
        self.assertNotIn("Listing", out.getvalue())
        self.assertFalse(Notification.objects.exists())
        out = io.StringIO()
        with patch.object(monitor, "full_scan") as fetch:
            call_command("monitor_avito_statuses", stdout=out)
            call_command("monitor_avito_statuses", status=True, stdout=out)
        fetch.assert_not_called()
        self.assertIn("AVITO_STATUS_MONITOR_READY", out.getvalue())


class AvitoFullScanTests(AvitoMonitorFixture, TestCase):
    # Full pagination is deliberately independent of UI's single-page snapshot.
    def setUp(self):
        super().setUp()
        self.token = patch.object(monitor, "access_token", return_value="test-token")
        self.token.start()
        self.addCleanup(self.token.stop)
        self.profile = patch.object(monitor.api, "_get", return_value={"id": 123})
        self.profile.start()
        self.addCleanup(self.profile.stop)

    def scan_pages(self, responses):
        with patch.object(monitor, "_page", side_effect=responses) as fetch:
            result = monitor.full_scan(self.connection)
        return result, fetch

    def test_follows_every_page_until_short_terminal_page(self):
        (items, pages), fetch = self.scan_pages([
            {"resources": [listing(x) for x in range(1, 51)], "meta": {"page": 1, "per_page": 50}},
            {"resources": [listing(51, "old")]},
        ])
        self.assertEqual((len(items), pages, fetch.call_count), (51, 2, 2))
        self.assertEqual(items["51"]["status"], "old")

    def test_exact_full_page_without_total_requires_extra_empty_page(self):
        (_, pages), fetch = self.scan_pages([{"resources": [listing(x) for x in range(50)]}, {"resources": []}])
        self.assertEqual((pages, fetch.call_count), (2, 2))

    def test_late_failure_never_commits_partial_baseline(self):
        with patch.object(monitor, "_page", side_effect=[{"resources": [listing(x) for x in range(50)]}, AvitoError("provider_http_503")]):
            self.assertEqual(monitor.scan_monitor(self.state.pk), ("failed", 0))
        self.state.refresh_from_db()
        self.assertIsNone(self.state.baseline_at)
        self.assertFalse(AvitoListingStatus.objects.exists())

    def test_malformed_duplicate_changed_total_unknown_status_or_oversize_fails_closed(self):
        cases = [
            [{"resources": [listing(1), listing(1)]}],
            [{"resources": [listing(x) for x in range(51)]}],
            [{"resources": [listing(1, "unknown")]}],
            [{"resources": [listing()], "meta": {"total": 2}}],
            [{"resources": [listing()], "meta": {"page": 2}}],
            [{"unexpected": []}],
            [{"resources": [], "meta": {"total": True}}],
            [{"resources": [listing(x) for x in range(50)], "meta": {"total": 51}},
             {"resources": [listing(51)], "meta": {"total": 52}}],
        ]
        for responses in cases:
            with self.subTest(responses=responses), self.assertRaises(AvitoError):
                self.scan_pages(responses)

    def test_account_mismatch_blocks_all_item_requests(self):
        self.connection.external_id = "999"
        with patch.object(monitor, "_page") as page, self.assertRaisesMessage(AvitoError, "provider_account_mismatch"):
            monitor.full_scan(self.connection)
        page.assert_not_called()

    def test_page_bound_is_failure_not_truncated_success(self):
        with patch.object(monitor, "MAX_PAGES", 1), self.assertRaisesMessage(AvitoError, "monitor_scan_limit"):
            self.scan_pages([{"resources": [listing(x) for x in range(50)]}])

    def test_shared_throttle_used_for_all_statuses_and_429_not_retried_inline(self):
        with patch.object(monitor.api, "_get", return_value={"resources": []}) as get:
            monitor._page("token", "123", 1, monitor.time.monotonic() + 20)
        self.assertEqual(AvitoApiThrottle.objects.count(), 1)
        self.assertIn("status=active%2Cremoved%2Cold%2Cblocked%2Crejected", get.call_args.args[1])
        with self.assertRaises(monitor.api.AvitoCooldownError):
            monitor.api.enforce_rate("123", "items", seconds=3)
