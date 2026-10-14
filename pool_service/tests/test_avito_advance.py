from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import TestCase, SimpleTestCase, Client
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_advance as advance
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import AvitoCredential, CommunicationAccess
from pool_service.models import Notification, OrganizationAccess, Profile
from pool_service.tests.test_avito_management import AvitoManagementTests


class AdvanceParsingTests(SimpleTestCase):
    def test_kopeks_exact_threshold_zero_negative_and_missing_are_distinct(self):
        for value, amount in ((10000, "100.00"), (1, "0.01"), (0, "0.00"), (-1, "-0.01")):
            data = advance.parse_balance({"balance": value, "PRIVATE": "SECRET"})
            self.assertEqual(amount, data["amount"])
            self.assertEqual(value, data["kopeks"])
            self.assertNotIn("SECRET", str(data))
            self.assertEqual(data, advance.for_display(data))
        for value in ({}, {"balance": None}, {"balance": True}, {"balance": "0"},
                      {"balance": 0.0}, {"balance": 10**15},
                      {"balance": 0, "error": {"message": "PRIVATE"}}):
            with self.assertRaises(AvitoError):
                advance.parse_balance(value)
        self.assertEqual({}, advance.for_display({"amount": "0.00", "source": "wallet", "currency": "RUB"}))

    def test_read_contract_uses_empty_body_source_header_and_conservative_rate(self):
        with patch("pool_service.communication_avito._provider_root", return_value="https://api.avito.ru"), \
             patch("pool_service.communication_avito._json_request", return_value={"balance": 9999}) as call, \
             patch.object(advance.avito_workspace, "enforce_rate") as rate:
            self.assertEqual("99.99", advance.fetch("synthetic-token", "123")["amount"])
        args, kwargs = call.call_args
        self.assertEqual("https://api.avito.ru/cpa/v3/balanceInfo", args[0])
        self.assertEqual(("POST", b"{}", 8), (kwargs["method"], kwargs["data"], kwargs["timeout"]))
        self.assertEqual("Service2", kwargs["headers"]["X-Source"])
        rate.assert_called_once_with("123", "cpa_balance", seconds=60)
        with patch("pool_service.communication_avito._provider_root", return_value="https://foreign.invalid"), \
             patch("pool_service.communication_avito._json_request") as call, \
             patch.object(advance.avito_workspace, "enforce_rate") as rate, \
             self.assertRaisesMessage(AvitoError, "advance_provider_invalid"):
            advance.fetch("synthetic-token", "123")
        call.assert_not_called()
        rate.assert_not_called()


class AdvanceMonitorTests(TestCase):
    def setUp(self):
        AvitoManagementTests.setUp(self)
        scheduler = patch.object(advance, "scheduler_state", return_value={
            "ready": True, "detail": "Synthetic scheduler", "code": "ready"})
        scheduler.start()
        self.addCleanup(scheduler.stop)

    def enable(self):
        return advance.configure(self.connection, self.owner, "enable")

    def poll(self, kopeks=15000, effect=None):
        advance.configure(self.connection, self.owner, "retry")
        with patch.object(advance, "access_token", return_value="synthetic-token"), \
             patch.object(advance.avito_workspace, "_get", return_value={"id": 123}), \
             patch.object(advance, "fetch", side_effect=effect,
                          return_value=advance.parse_balance({"balance": kopeks})):
            return advance.scan_connection(self.connection.pk)

    def snapshot(self):
        self.connection.refresh_from_db()
        return self.connection.settings["avito_workspace"]["sections"]["advance"]

    def test_two_thresholds_no_repeat_and_rearm_after_topup(self):
        self.enable()
        for value, expected in ((15000, 0), (10000, 1), (5000, 1), (0, 2), (-1, 2),
                                (5000, 3), (0, 4), (15000, 4), (10000, 5)):
            self.assertEqual("success", self.poll(value))
            self.assertEqual(expected, Notification.objects.count())
        self.assertEqual(["warning", "danger", "warning", "danger", "warning"],
                         list(Notification.objects.order_by("pk").values_list("level", flat=True)))
        self.assertEqual({self.owner.pk}, set(Notification.objects.values_list("user_id", flat=True)))
        self.assertEqual({self.org.pk}, set(Notification.objects.values_list("organization_id", flat=True)))
        self.assertNotIn("synthetic-token", str(self.connection.settings))

    def test_initial_empty_or_jump_to_empty_sends_only_empty_alert(self):
        self.enable()
        self.assertEqual("success", self.poll(0))
        self.assertEqual(1, Notification.objects.count())
        self.assertIn("закончился", Notification.objects.get().title)
        self.poll(20000)
        self.poll(0)
        self.assertEqual(2, Notification.objects.count())

    def test_provider_failure_preserves_amount_and_zone_without_false_zero(self):
        self.enable()
        self.poll(15000)
        previous = self.snapshot()
        self.assertEqual("failed", self.poll(effect=AvitoError("provider_http_403")))
        current = self.snapshot()
        self.assertEqual(previous["data"], current["data"])
        self.assertEqual(previous["success_at"], current["success_at"])
        self.assertTrue(current["stale"])
        self.assertEqual(0, Notification.objects.count())
        self.connection.refresh_from_db()
        self.assertEqual("healthy", self.connection.settings[advance.KEY]["last_zone"])

    def test_revoked_recipient_keys_account_or_disabled_connection_block_notifications(self):
        for mutation in ("capability", "keys", "account", "disabled"):
            with self.subTest(mutation=mutation):
                CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(can_manage_channels=True)
                self.connection.refresh_from_db()
                self.connection.is_active = True
                self.connection.external_id = "123"
                self.connection.save()
                self.enable()
                def mutate(*_args):
                    if mutation == "capability":
                        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(can_manage_channels=False)
                    elif mutation == "keys":
                        AvitoCredential.objects.filter(connection=self.connection).update(client_secret_encrypted="changed-private")
                    elif mutation == "account":
                        self.connection.external_id = "999"
                        self.connection.save(update_fields=["external_id"])
                    else:
                        self.connection.is_active = False
                        self.connection.save(update_fields=["is_active"])
                    return advance.parse_balance({"balance": 0})
                self.assertEqual("failed", self.poll(effect=mutate))
                self.assertFalse(Notification.objects.exists())
                self.connection.refresh_from_db()
                self.assertFalse(self.connection.settings[advance.KEY]["enabled"])

    def test_other_admin_cannot_take_over_subscription_and_preferences_are_respected(self):
        self.enable()
        other = User.objects.create_user("synthetic-other-owner")
        OrganizationAccess.objects.create(user=other, organization=self.org, role="owner")
        for action in ("enable", "disable", "retry"):
            with self.assertRaises(PermissionDenied):
                advance.configure(self.connection, other, action)
        Profile.objects.update_or_create(user=self.owner, defaults={"in_app_notifications_enabled": False})
        with patch("pool_service.services.notifications.send_push_to_users") as push:
            self.poll(0)
        self.assertFalse(Notification.objects.exists())
        push.assert_not_called()

    def test_shared_lease_and_worker_budget_protect_other_reads(self):
        self.enable()
        self.connection.refresh_from_db()
        data = dict(self.connection.settings)
        data["avito_workspace_refresh_until"] = (timezone.now() + timedelta(minutes=2)).isoformat()
        self.connection.settings = data
        self.connection.save()
        with patch.object(advance, "fetch") as fetch:
            self.assertEqual("skipped", advance.scan_connection(self.connection.pk))
        fetch.assert_not_called()
        with patch.object(advance, "scan_connection") as scan, \
             patch("time.monotonic", return_value=100):
            advance.scan_due(deadline=149, limit=2)
        scan.assert_not_called()

    def test_csrf_org_scope_manual_refresh_and_get_without_api(self):
        self.client.force_login(self.owner)
        url = reverse("avito_refresh_data", args=[self.connection.pk])
        self.client.post(url, {"section": "advance_enable", "actor_id": self.manager.pk})
        self.connection.refresh_from_db()
        self.assertEqual(self.owner.pk, self.connection.settings[advance.KEY]["actor_id"])
        with patch("pool_service.avito_management.access_token", return_value="synthetic-token"), \
             patch("pool_service.avito_workspace._get", return_value={"id": 123}), \
             patch.object(advance, "fetch", return_value=advance.parse_balance({"balance": 10000})):
            self.client.post(url, {"section": "advance"})
            self.client.post(url, {"section": "advance"})
        self.assertEqual(1, Notification.objects.count())
        with patch.object(advance, "fetch") as fetch:
            page = self.client.get(self.url)
        fetch.assert_not_called()
        self.assertContains(page, "Аванс")
        self.assertContains(page, "100.00")
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(403, csrf.post(url, {"section": "advance_enable"}).status_code)
        self.client.force_login(self.manager)
        self.assertEqual(403, self.client.post(url, {"section": "advance_enable"}).status_code)

    def test_key_rotation_during_first_identity_discards_old_token_balance_even_if_rebound(self):
        self.enable()
        self.client.force_login(self.owner)
        calls = [0]
        def initial_self(_token, _path):
            calls[0] += 1
            if calls[0] == 1:
                AvitoCredential.objects.filter(connection=self.connection).update(client_secret_encrypted="rotated-private")
                advance.configure(self.connection, self.owner, "enable")
            return {"id": 123}
        with patch("pool_service.avito_management.access_token", return_value="old-synthetic-token"), \
             patch("pool_service.avito_workspace._get", side_effect=initial_self), \
             patch.object(advance, "fetch", return_value=advance.parse_balance({"balance": 0})):
            self.client.post(reverse("avito_refresh_data", args=[self.connection.pk]), {"section": "advance"})
        self.assertFalse(Notification.objects.exists())
        state = self.snapshot()
        self.assertTrue(state["stale"])
        self.assertNotIn("data", state)
        self.connection.refresh_from_db()
        self.assertNotIn("last_zone", self.connection.settings[advance.KEY])
