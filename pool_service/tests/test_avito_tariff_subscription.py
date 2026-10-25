"""Synthetic subscription evidence; no agency or customer request is sent."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
import copy

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.test import Client as TestClient, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_tariff_subscription as tariff
from pool_service import avito_tariff_reminder as calendar
from pool_service.communication_models import (
    AvitoCredential, ChannelConnection, CommunicationAccess, CommunicationChannel)
from pool_service.communication_services import conversation_capability
from pool_service.models import Organization, OrganizationAccess


class TariffSubscriptionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic tariff organization",
            paid_until=timezone.now() + timedelta(days=365))
        self.other_org = Organization.objects.create(name="Foreign tariff organization")
        self.owner = User.objects.create_user("tariff-api-owner")
        self.manager = User.objects.create_user("tariff-api-manager")
        self.foreign = User.objects.create_user("tariff-api-foreign")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role="manager")
        OrganizationAccess.objects.create(organization=self.other_org, user=self.foreign, role="owner")
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Services")
        self.connection = ChannelConnection.objects.create(pk=4, channel=self.channel,
            name="Services", external_id="446312402")
        self.goods = ChannelConnection.objects.create(pk=1, channel=self.channel,
            name="Goods", external_id="goods-synthetic", settings={"untouched": True})
        AvitoCredential.objects.create(connection=self.connection, client_id_encrypted="synthetic-cipher-id",
            client_secret_encrypted="synthetic-cipher-secret")
        self.http = patch("pool_service.communication_avito._json_request",
            side_effect=AssertionError("No live provider API in these tests"))
        self.http.start()
        self.addCleanup(self.http.stop)

    def normalized(self, rows=None, **changes):
        values = dict(account_id="446312402", account_matches=1, pages_complete=True,
            fetched_at=timezone.now())
        values.update(changes)
        return tariff.normalize_services_subscription(
            [{"category": "services", "level": "extended", "isActive": True}] if rows is None else rows,
            **values)

    def manual(self):
        return calendar.configure(self.connection, self.owner, "save_date",
            expires_on=timezone.localdate() + timedelta(days=30))

    def saved(self):
        self.connection.refresh_from_db()
        return self.connection.settings

    def store_synthetic_success(self, observation=None):
        _current, binding = tariff._scope(4, self.owner)
        with patch.object(tariff, "CONTRACT_STATUS", "verified"):
            return tariff.record_observation(self.connection, self.owner,
                observation or self.normalized(), binding)

    def test_only_explicit_boolean_activity_drives_state_not_expiry_or_client_status(self):
        for raw, expected in ((True, "active"), (False, "inactive"), (None, "status_unknown"),
                              (0, "status_unknown"), (1, "status_unknown"), ("false", "status_unknown")):
            with self.subTest(raw=raw):
                value = self.normalized([{"category": "services", "isActive": raw,
                    "expiredTo": "1900-01-01", "clientStatus": "inactive"}])
                self.assertEqual(value.state, expected)
                self.assertEqual(value.expiry_state, "date_unconfirmed")
                self.assertFalse(hasattr(value, "expires_on"))
                self.assertNotIn("1900", str(value))
        self.assertEqual(self.normalized().expiry_state, "absent")

    def test_absent_account_subscription_or_activity_are_distinct_not_inactive(self):
        self.assertEqual(self.normalized([], account_matches=0).state, "account_not_returned")
        self.assertEqual(self.normalized([]).state, "subscription_missing")
        self.assertEqual(self.normalized([None]).state, "subscription_missing")
        self.assertEqual(self.normalized([{"category": "services"}]).state, "status_unknown")

    def test_wrong_account_category_partial_pages_and_duplicate_records_fail_closed(self):
        row = {"category": "services", "isActive": True}
        for rows, changes, code in (
                ([row], {"account_id": "other"}, "tariff_account_mismatch"),
                ([row], {"pages_complete": False}, "tariff_pages_incomplete"),
                ([row], {"pages_complete": None}, "tariff_pages_incomplete"),
                ([row], {"account_matches": 2}, "tariff_ambiguous"),
                ([row, row], {}, "tariff_ambiguous"),
                ([row, {**row, "isActive": False}], {}, "tariff_ambiguous"),
                ([{"category": "goods", "isActive": False}], {}, "tariff_category_mismatch"),
                ([row], {"account_matches": 0}, "tariff_account_mismatch")):
            with self.subTest(code=code, changes=changes):
                with self.assertRaisesMessage(tariff.SubscriptionError, code):
                    self.normalized(rows, **changes)

    def test_missing_naive_future_or_old_fetch_time_cannot_be_a_success(self):
        for stamp in (None, "not-a-time", timezone.now().replace(tzinfo=None),
                      timezone.now() + timedelta(minutes=1), timezone.now() - timedelta(minutes=11)):
            with self.subTest(stamp=stamp):
                with self.assertRaisesMessage(tariff.SubscriptionError, "tariff_data_invalid"):
                    self.normalized(fetched_at=stamp)

    def test_blocked_refresh_does_not_call_api_overwrite_manual_or_touch_goods(self):
        self.manual()
        before = copy.deepcopy(self.saved())
        result = tariff.refresh(self.connection, self.owner)
        self.assertEqual(result["code"], "method_contract_unavailable")
        self.assertFalse(result["request_performed"])
        after = self.saved()
        self.assertEqual(after[calendar.KEY], before[calendar.KEY])
        self.assertNotIn("last_success_at", after[tariff.KEY])
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {"untouched": True})

    def test_identical_blocked_retry_does_not_manufacture_a_new_check_time(self):
        tariff.refresh(self.connection, self.owner)
        first = copy.deepcopy(self.saved()[tariff.KEY])
        tariff.refresh(self.connection, self.owner)
        self.assertEqual(self.saved()[tariff.KEY], first)

    def test_manager_foreign_owner_goods_inactive_actor_and_revoked_channel_capability_cannot_write(self):
        for connection, actor in ((self.connection, self.manager),
                                   (self.connection, self.foreign), (self.goods, self.owner)):
            with self.subTest(actor=actor.pk, connection=connection.pk):
                with self.assertRaises(PermissionDenied):
                    tariff.refresh(connection, actor)
        User.objects.filter(pk=self.owner.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            tariff.refresh(self.connection, self.owner)
        User.objects.filter(pk=self.owner.pk).update(is_active=True)
        OrganizationAccess.objects.filter(user=self.owner, organization=self.org).update(role="manager")
        # A role change does not revoke an explicitly delegated communication ACL.
        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(
            can_manage_channels=False)
        with self.assertRaises(PermissionDenied):
            tariff.refresh(self.connection, self.owner)
        self.assertNotIn(tariff.KEY, self.saved())

    def test_delegated_manager_capability_is_allowed_until_revoked_in_the_selected_org(self):
        self.assertFalse(conversation_capability(self.manager, "can_manage_channels", self.org))
        CommunicationAccess.objects.filter(user=self.manager, organization=self.org).update(
            can_manage_channels=True)
        self.assertTrue(conversation_capability(self.manager, "can_manage_channels", self.org))
        current, _binding = tariff._scope(4, self.manager)
        self.assertEqual(current.channel.organization_id, self.org.pk)
        result = tariff.refresh(self.connection, self.manager)
        self.assertEqual(result["code"], "method_contract_unavailable")
        self.assertFalse(result["request_performed"])
        before = copy.deepcopy(self.saved())
        # A capability in another organization cannot rescue the revoked local ACL.
        OrganizationAccess.objects.create(user=self.manager, organization=self.other_org, role="owner")
        CommunicationAccess.objects.filter(user=self.manager, organization=self.other_org).update(
            can_manage_channels=True)
        CommunicationAccess.objects.filter(user=self.manager, organization=self.org).update(
            can_manage_channels=False)
        self.assertTrue(conversation_capability(self.manager, "can_manage_channels", self.other_org))
        self.assertFalse(conversation_capability(self.manager, "can_manage_channels", self.org))
        with self.assertRaises(PermissionDenied):
            tariff.refresh(self.connection, self.manager)
        self.assertEqual(tariff.display_state(self.connection, self.manager), {})
        self.assertEqual(self.saved(), before)

    def test_pending_contract_cannot_record_a_success_even_with_normalized_data(self):
        _current, binding = tariff._scope(4, self.owner)
        with self.assertRaisesMessage(tariff.SubscriptionError, "method_contract_unavailable"):
            tariff.record_observation(self.connection, self.owner, self.normalized(), binding)
        self.assertNotIn(tariff.KEY, self.saved())

    def test_synthetic_success_and_later_error_keep_source_and_manual_evidence_independent(self):
        self.manual()
        manual = copy.deepcopy(self.saved()[calendar.KEY])
        success = self.store_synthetic_success()
        self.assertEqual(success["source"], tariff.SOURCE)
        tariff.refresh(self.connection, self.owner)
        state = self.saved()[tariff.KEY]
        self.assertEqual(state["data"], success["data"])
        self.assertEqual(state["last_success_at"], success["last_success_at"])
        self.assertEqual(self.saved()[calendar.KEY], manual)
        display = tariff.display_state(self.connection, self.owner)
        self.assertEqual(display["observed_state"], "active")
        self.assertEqual(display["state"], "method_contract_unavailable")
        self.assertFalse(display["dates_comparable"])
        self.assertTrue(display["manual_date_present"])

    def test_binding_and_inconsistent_cached_state_do_not_leak_old_success(self):
        self.store_synthetic_success()
        metadata = self.saved()
        metadata[tariff.KEY]["data"]["is_active"] = None
        self.connection.settings = metadata
        self.connection.save(update_fields=["settings"])
        shown = tariff.display_state(self.connection, self.owner)
        self.assertIsNone(shown["last_success_at"])
        self.assertEqual(shown["code"], "tariff_data_invalid")
        self.assertIsNone(shown["is_active"])
        self.store_synthetic_success()
        credential = AvitoCredential.objects.get(connection=self.connection)
        credential.client_secret_encrypted = "rotated-synthetic-secret"
        credential.save(update_fields=["client_secret_encrypted"])
        shown = tariff.display_state(self.connection, self.owner)
        self.assertIsNone(shown["last_success_at"])
        self.assertEqual(shown["state"], "access_unverified")

    def test_malformed_json_cache_codes_and_state_fields_never_break_dashboard(self):
        self.store_synthetic_success()
        original = copy.deepcopy(self.saved())
        for raw in ([], {}, ["PRIVATE-PAYLOAD"], {"unsafe": "PRIVATE-PAYLOAD"}):
            for field in ("last_attempt_code", "state", "expiry_state"):
                with self.subTest(field=field, raw=raw):
                    metadata = copy.deepcopy(original)
                    if field == "last_attempt_code":
                        metadata[tariff.KEY][field] = raw
                    else:
                        metadata[tariff.KEY]["data"][field] = raw
                    self.connection.settings = metadata
                    self.connection.save(update_fields=["settings"])
                    shown = tariff.display_state(self.connection, self.owner)
                    self.assertIn(shown["code"], {"tariff_read_failed", "tariff_data_invalid"})
                    self.assertNotIn("PRIVATE-PAYLOAD", str(shown))
                    if field != "last_attempt_code":
                        self.assertIsNone(shown["last_success_at"])
                        self.assertIsNone(shown["is_active"])
                    self.assertIn("tariff_read_failed", tariff.support_question(raw))
        self.client.force_login(self.owner)
        response = self.client.get(reverse("avito_dashboard"), {"account": 4})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "PRIVATE-PAYLOAD")

    def test_stale_data_and_http_denial_are_separate_from_inactive_or_expired(self):
        self.store_synthetic_success()
        metadata = self.saved()
        metadata[tariff.KEY]["last_success_at"] = (timezone.now() - timedelta(days=3)).isoformat()
        metadata[tariff.KEY]["last_attempt_code"] = "provider_http_403"
        self.connection.settings = metadata
        self.connection.save(update_fields=["settings"])
        shown = tariff.display_state(self.connection, self.owner)
        self.assertEqual(shown["state"], "unavailable")
        self.assertEqual(shown["observed_state"], "active")
        self.assertTrue(shown["stale"])
        self.assertNotIn("expired", shown["state"])
        self.assertIn("provider_http_403", shown["support_draft"])

    def test_record_rejects_changed_binding_and_inconsistent_observation(self):
        _current, binding = tariff._scope(4, self.owner)
        with patch.object(tariff, "CONTRACT_STATUS", "verified"):
            with self.assertRaisesMessage(tariff.SubscriptionError, "tariff_scope_changed"):
                tariff.record_observation(self.connection, self.owner, self.normalized(),
                    {**binding, "organization_id": self.other_org.pk})
            with self.assertRaisesMessage(tariff.SubscriptionError, "tariff_data_invalid"):
                tariff.record_observation(self.connection, self.owner,
                    replace(self.normalized(), is_active=None), binding)
        self.assertNotIn(tariff.KEY, self.saved())

    def test_setup_receipt_and_dashboard_get_are_read_only_and_post_requires_csrf(self):
        before = copy.deepcopy(self.saved())
        receipt = tariff.setup_evidence(self.owner)
        self.assertEqual(receipt["state"], "blocked")
        self.assertFalse(receipt["request_performed"])
        self.assertEqual(self.saved(), before)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("avito_dashboard"), {"account": 4})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "client_credentials")
        self.assertEqual(self.saved(), before)
        strict = TestClient(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        url = reverse("avito_refresh_data", args=[4])
        self.assertEqual(strict.post(url, {"section": "tariff_api_refresh"}).status_code, 403)
        self.assertEqual(self.client.post(url, {"section": "tariff_api_refresh"}).status_code, 302)
        self.assertEqual(self.saved()[tariff.KEY]["last_attempt_code"], "method_contract_unavailable")
