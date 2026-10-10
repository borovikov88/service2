import io
import time
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.management import call_command, CommandError
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_autoload_auto as auto, avito_autoload_report, avito_workspace
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import AvitoCredential, AvitoStatusMonitor, ChannelConnection, CommunicationChannel
from pool_service.models import Notification, Organization, OrganizationAccess


def report():
    return {"version": 1, "kind": "last_successful", "complete": True, "upload_id": "123",
            "upload_finished": True, "upload_status": "success_warning", "rows": [],
            "events": [], "total": 0, "pages": 1, "counts": {}}


class AutoloadAutoTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Auto report test")
        self.owner = User.objects.create_user("auto-report-owner")
        self.manager = User.objects.create_user("auto-report-manager")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        OrganizationAccess.objects.create(user=self.manager, organization=self.org, role="manager")
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Avito")
        self.connection = ChannelConnection.objects.create(channel=channel, external_id="123")
        self.credential = AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted="synthetic-id", client_secret_encrypted="synthetic-secret")
        self.url = reverse("avito_configure_autoload_auto", args=[self.connection.pk])
        self.client.force_login(self.owner)
        for target, value in (("pool_service.avito_autoload_auto.scheduler_state", {"ready": True, "detail": "Test tick"}),
                              ("pool_service.avito_autoload_auto.access_token", "synthetic-token"),
                              ("pool_service.avito_workspace._get", {"id": 123})):
            mock = patch(target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        auto.configure(self.connection, self.owner, "enable")

    def state(self):
        self.connection.refresh_from_db()
        return self.connection.settings[auto.KEY]

    def scan(self, data=None):
        with patch.object(avito_autoload_report, "fetch_report", return_value=data if data is not None else report()) as fetch:
            status = auto.scan_connection(self.connection.pk)
        return status, fetch

    def due(self):
        self.connection.refresh_from_db()
        self.connection.settings[auto.KEY]["next_due_at"] = (timezone.now() - timedelta(seconds=1)).isoformat()
        self.connection.save(update_fields=["settings"])

    def snapshot(self):
        self.connection.refresh_from_db()
        return self.connection.settings["avito_workspace"]["sections"]["autoload_report"]

    def test_first_success_uses_account_keys_saves_full_report_and_does_not_notify(self):
        status, fetch = self.scan()
        self.assertEqual(status, "success")
        fetch.assert_called_once_with("synthetic-token", "123", kind="last_successful")
        self.assertEqual(self.snapshot()["data"], report())
        self.assertIsNotNone(self.state()["last_success_at"])
        self.assertGreater(auto._date(self.state()["next_due_at"]), timezone.now() + timedelta(minutes=59))
        self.assertFalse(Notification.objects.exists())
        self.assertEqual(self.scan()[0], "skipped")

    def test_failed_provider_or_partial_report_retains_last_success_and_retries_later(self):
        self.scan()
        previous = self.snapshot()
        for failure in (AvitoError("provider_http_403"), RuntimeError("private-key"), None):
            self.due()
            if failure is None:
                mock = patch.object(avito_autoload_report, "fetch_report", return_value={"complete": False})
            else:
                mock = patch.object(avito_autoload_report, "fetch_report", side_effect=failure)
            with mock:
                self.assertEqual(auto.scan_connection(self.connection.pk), "failed")
            now = self.snapshot()
            self.assertEqual(now["data"], previous["data"])
            self.assertEqual(now["success_at"], previous["success_at"])
            self.assertTrue(now["stale"])
            self.assertNotIn("private-key", str(self.state()))
            self.assertGreater(auto._date(self.state()["next_due_at"]), timezone.now() + timedelta(minutes=14))

    def test_missing_ready_tick_rejects_activation_without_provider_calls(self):
        auto.configure(self.connection, self.owner, "disable")
        with patch.object(auto, "scheduler_state", return_value={"ready": False}), patch.object(avito_autoload_report, "fetch_report") as fetch:
            with self.assertRaises(ValueError):
                auto.configure(self.connection, self.owner, "enable")
        fetch.assert_not_called()
        self.assertFalse(self.state()["enabled"])

    def test_owner_revocation_or_changed_keys_prevent_fetch(self):
        for change in ("role", "keys"):
            with self.subTest(change=change):
                if change == "role":
                    OrganizationAccess.objects.filter(user=self.owner).update(role="manager")
                else:
                    AvitoCredential.objects.filter(pk=self.credential.pk).update(client_secret_encrypted="synthetic-new-secret")
                with patch.object(avito_autoload_report, "fetch_report") as fetch:
                    self.assertEqual(auto.scan_connection(self.connection.pk), "failed")
                fetch.assert_not_called()
                self.assertFalse(self.state()["enabled"])
                OrganizationAccess.objects.filter(user=self.owner).update(role="owner")
                auto.configure(self.connection, self.owner, "enable")

    def test_token_rotation_does_not_disable_audit(self):
        AvitoCredential.objects.filter(pk=self.credential.pk).update(
            access_token_encrypted="synthetic-refreshed-token", updated_at=timezone.now())
        self.assertEqual(self.scan()[0], "success")
        self.assertTrue(self.state()["enabled"])

    def test_account_mismatch_cannot_fetch_report(self):
        with patch.object(avito_workspace, "_get", return_value={"id": 999}), patch.object(avito_autoload_report, "fetch_report") as fetch:
            self.assertEqual(auto.scan_connection(self.connection.pk), "failed")
        fetch.assert_not_called()
        self.assertEqual(self.state()["last_error_code"], "provider_account_mismatch")

    def test_inflight_disable_reenable_move_role_or_keys_discard_result(self):
        def change(mode):
            def changed(*args, **kwargs):
                if mode == "disable":
                    auto.configure(self.connection, self.owner, "disable")
                elif mode == "reenable":
                    auto.configure(self.connection, self.owner, "disable")
                    auto.configure(self.connection, self.owner, "enable")
                elif mode == "move":
                    ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="999")
                elif mode == "role":
                    OrganizationAccess.objects.filter(user=self.owner).update(role="manager")
                else:
                    AvitoCredential.objects.filter(pk=self.credential.pk).update(client_id_encrypted="synthetic-new-id")
                return report()
            return changed
        for mode in ("disable", "reenable", "move", "role", "keys"):
            with self.subTest(mode=mode):
                with patch.object(avito_autoload_report, "fetch_report", side_effect=change(mode)):
                    self.assertIn(auto.scan_connection(self.connection.pk), ("skipped", "failed"))
                self.connection.refresh_from_db()
                self.assertNotIn("avito_workspace", self.connection.settings)
                ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="123")
                OrganizationAccess.objects.filter(user=self.owner).update(role="owner")
                self.connection.refresh_from_db()
                auto.configure(self.connection, self.owner, "disable")
                auto.configure(self.connection, self.owner, "enable")

    def test_shared_ui_lease_and_expired_lease_recovery(self):
        self.connection.refresh_from_db()
        self.connection.settings.update(avito_workspace_refresh_lease="ui",
            avito_workspace_refresh_until=(timezone.now() + timedelta(minutes=2)).isoformat())
        self.connection.save(update_fields=["settings"])
        self.assertEqual(self.scan()[0], "skipped")
        self.connection.settings["avito_workspace_refresh_until"] = (timezone.now() - timedelta(seconds=1)).isoformat()
        self.connection.save(update_fields=["settings"])
        self.assertEqual(self.scan()[0], "success")

    def test_lease_expiring_during_fetch_does_not_save_success(self):
        def expired(*args, **kwargs):
            self.connection.refresh_from_db()
            self.connection.settings["avito_workspace_refresh_until"] = (timezone.now() - timedelta(seconds=1)).isoformat()
            self.connection.save(update_fields=["settings"])
            return report()
        with patch.object(avito_autoload_report, "fetch_report", side_effect=expired):
            self.assertEqual(auto.scan_connection(self.connection.pk), "failed")
        self.assertNotIn("data", self.snapshot())

    def test_org_scoped_csrf_protected_post_and_get_reads_cache_only(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(self.url, {"action": "disable"}).status_code, 403)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(self.url, {"action": "enable"}).status_code, 403)
        foreign = Organization.objects.create(name="Other scope")
        user = User.objects.create_user("auto-report-other-owner")
        OrganizationAccess.objects.create(user=user, organization=foreign, role="owner")
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.url, {"action": "enable"}).status_code, 404)
        self.client.force_login(self.owner)
        with patch.object(avito_autoload_report, "fetch_report") as fetch:
            self.assertContains(self.client.get(reverse("avito_dashboard")), "Почасовое чтение отчётов")
            self.client.post(self.url, {"action": "retry"})
        fetch.assert_not_called()

    def test_due_only_bounded_command_and_status_no_provider_calls(self):
        with patch.object(avito_autoload_report, "fetch_report", return_value=report()) as fetch:
            counts = auto.scan_due(deadline=time.monotonic() + 100)
            self.assertEqual(counts["success"], 1)
            self.assertEqual(auto.scan_due(deadline=time.monotonic() + 100)["success"], 0)
        self.assertEqual(fetch.call_count, 1)
        self.due()
        with patch.object(avito_autoload_report, "fetch_report") as fetch:
            self.assertEqual(auto.scan_due(deadline=time.monotonic() + 1)["success"], 0)
            call_command("monitor_avito_statuses", status=True, stdout=io.StringIO())
        fetch.assert_not_called()
        out = io.StringIO()
        with patch.object(avito_autoload_report, "fetch_report", side_effect=AvitoError("provider_http_403")):
            with self.assertRaises(CommandError):
                call_command("monitor_avito_statuses", stdout=out)
        self.assertIn("AVITO_AUTOLOAD_REPORT success=0 failed=1 skipped=0", out.getvalue())
        self.assertNotIn("synthetic-", out.getvalue())
    
    def test_report_failure_does_not_skip_status_scan_and_short_budget_reserves_it(self):
        subscription = AvitoStatusMonitor.objects.create(connection=self.connection, organization=self.org,
            recipient=self.owner, account_id="123", enabled=True)
        target = "pool_service.management.commands.monitor_avito_statuses.scan_monitor"
        with patch(target, return_value=("baseline", 0)) as scan, patch.object(avito_autoload_report, "fetch_report") as fetch:
            call_command("monitor_avito_statuses", budget_seconds=450, stdout=io.StringIO())
        scan.assert_called_once_with(subscription.pk)
        fetch.assert_not_called()
        with patch(target, return_value=("baseline", 0)) as scan, patch.object(avito_autoload_report, "fetch_report", side_effect=AvitoError("provider_http_403")):
            with self.assertRaises(CommandError):
                call_command("monitor_avito_statuses", stdout=io.StringIO())
        scan.assert_called_once_with(subscription.pk)

    def test_status_scan_survives_full_report_request_cost_with_short_shared_budget(self):
        subscription = AvitoStatusMonitor.objects.create(connection=self.connection, organization=self.org,
            recipient=self.owner, account_id="123", enabled=True)
        for budget, expected_fetches in ((525, 0), (550, 1)):
            with self.subTest(budget=budget):
                self.due()
                clock = [100.0]
                def slow_report(*args, **kwargs):
                    # token15 + profile8 + report55 + finalHTTP8, all accounted here.
                    clock[0] += 86
                    return report()
                with patch("pool_service.management.commands.monitor_avito_statuses.time.monotonic", side_effect=lambda: clock[0]), patch("pool_service.management.commands.monitor_avito_statuses.scan_monitor", return_value=("baseline", 0)) as scan, patch.object(avito_autoload_report, "fetch_report", side_effect=slow_report) as fetch:
                    call_command("monitor_avito_statuses", budget_seconds=budget, stdout=io.StringIO())
                self.assertEqual(fetch.call_count, expected_fetches)
                scan.assert_called_once_with(subscription.pk)
