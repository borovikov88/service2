"""Synthetic agency-reader contracts. No live API or company amounts."""
import copy
import json
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import connection as database
from django.test import Client as TestClient, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_tariff_subscription as tariff
from pool_service import avito_tariff_reminder as calendar
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import (
    AvitoCredential, ChannelConnection, CommunicationAccess, CommunicationChannel)
from pool_service.models import Organization, OrganizationAccess


@override_settings(AVITO_API_ROOT="https://api.avito.ru")
class TariffSubscriptionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Synthetic tariff organization",
            paid_until=timezone.now() + timedelta(days=365))
        self.other_org = Organization.objects.create(name="Foreign tariff organization")
        self.owner = User.objects.create_user("tariff-reader-owner")
        self.manager = User.objects.create_user("tariff-reader-manager")
        self.foreign = User.objects.create_user("tariff-reader-foreign")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role="manager")
        OrganizationAccess.objects.create(organization=self.other_org, user=self.foreign, role="owner")
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Services")
        self.connection = ChannelConnection.objects.create(pk=4, channel=self.channel,
            name="Services", external_id=tariff.ACCOUNT)
        self.goods = ChannelConnection.objects.create(pk=1, channel=self.channel,
            name="Goods", external_id="synthetic-goods", settings={"untouched": True})
        self.credential = AvitoCredential.objects.create(connection=self.connection,
            client_id_encrypted="synthetic-id", client_secret_encrypted="synthetic-secret")
        self.real_clients_page = tariff._clients_page
        for name, kwargs in (
                ("access_token", {"return_value": "synthetic-token"}),
                ("authorized_account_id", {"return_value": tariff.ACCOUNT}),
                ("enforce_rate", {"return_value": None}),
                ("_clients_page", {"side_effect": AvitoError("provider_http_403")}),
                ("_json_request", {"side_effect": AssertionError("No live HTTP")})):
            mock = patch.object(tariff, name, **kwargs)
            setattr(self, name + "_mock", mock.start())
            self.addCleanup(mock.stop)

    def subscription(self, **changes):
        return {"category": "services", "level": "extended", "isActive": True, **changes}

    def client_row(self, identifier=None, subscription="default"):
        return {"clientId": tariff.ACCOUNT if identifier is None else identifier,
            "name": "PRIVATE-SYNTHETIC-CUSTOMER", "status": "inactive",
            "linkType": "synthetic-unused", "linkTime": {"unused": True},
            **({"subscription": self.subscription()} if subscription == "default"
               else {"subscription": subscription} if subscription is not None else {})}

    def response(self, rows=None, total=None):
        rows = [self.client_row()] if rows is None else rows
        return {"status": "ok", "result": {"clients": rows, "total": len(rows) if total is None else total}}

    def success(self, response=None):
        self._clients_page_mock.side_effect = None
        self._clients_page_mock.return_value = self.response() if response is None else response

    def saved(self):
        self.connection.refresh_from_db()
        return self.connection.settings

    def normalized(self, rows=None, **changes):
        values = dict(account_id=tariff.ACCOUNT, account_matches=1,
            pages_complete=True, fetched_at=timezone.now())
        values.update(changes)
        return tariff.normalize_services_subscription(
            [self.subscription()] if rows is None else rows, **values)

    def manual(self, days=30):
        return calendar.configure(self.connection, self.owner, "save_date",
            expires_on=timezone.localdate(timezone=tariff.ZONE) + timedelta(days=days))

    def store_success(self, observation=None):
        current, binding = tariff._scope(4, self.owner)
        return tariff.record_observation(current, self.owner,
            observation or self.normalized(), binding)

    def test_verified_format_is_not_proof_of_agency_access_and_get_is_read_only(self):
        before = copy.deepcopy(self.saved())
        shown = tariff.display_state(self.connection, self.owner)
        self.assertEqual(shown["contract_status"], "verified")
        self.assertEqual(shown["access_state"], "unverified")
        self.assertEqual(shown["state"], "access_unverified")
        self.assertEqual(tariff.setup_evidence(self.owner)["request_performed"], False)
        self.client.force_login(self.owner)
        response = self.client.get(reverse("avito_dashboard"), {"account": 4})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "client_credentials")
        self.assertNotContains(response, "полный контракт недоступен")
        self.assertEqual(self.saved(), before)
        self._clients_page_mock.assert_not_called()

    def test_activity_uses_only_explicit_subscription_boolean_and_known_level(self):
        for active in (True, False):
            value = self.normalized([self.subscription(isActive=active,
                expiredTo="1900-01-01T00:00:00Z", clientStatus="ignored")])
            self.assertEqual(value.state, "active" if active else "inactive")
            self.assertIs(value.is_active, active)
        for active in (None, 0, 1, "false", [], {}):
            with self.subTest(active=active), self.assertRaisesMessage(tariff.SubscriptionError, "tariff_data_invalid"):
                self.normalized([self.subscription(isActive=active)])
        for level in (None, "maximum", "", [], {}):
            with self.subTest(level=level), self.assertRaisesMessage(tariff.SubscriptionError, "tariff_data_invalid"):
                self.normalized([self.subscription(level=level)])

    def test_rfc3339_offset_z_and_optional_absence_are_separate_from_invalid_values(self):
        for stamp in ("2031-03-04T12:34:56Z", "2031-03-04T19:34:56+07:00", "2031-03-04t12:34:56z"):
            observed = self.normalized([self.subscription(expiredTo=stamp)])
            self.assertEqual(observed.expiry_state, "present")
            self.assertTrue(timezone.is_aware(observed.expires_at))
            self.assertEqual(observed.expires_at.astimezone(tariff.ZONE).hour, 19)
        self.assertEqual(self.normalized().expiry_state, "absent")
        for stamp in (None, "", "2031-03-04", "2031-03-04T12:34:56", "2031-03-04T12:34:56-00:00",
                      "2031-99-04T12:34:56Z", "9999-12-31T23:59:59-23:59", [], {"PRIVATE": True}):
            with self.subTest(stamp=stamp), self.assertRaisesMessage(tariff.SubscriptionError, "tariff_data_invalid"):
                self.normalized([self.subscription(expiredTo=stamp)])

    def test_absent_target_and_subscription_are_unknown_never_inactive(self):
        self.assertEqual(self.normalized([], account_matches=0).state, "account_not_returned")
        self.assertEqual(self.normalized([None]).state, "subscription_missing")
        for rows, changes in (([self.subscription()], {"account_id": "999"}),
                              ([self.subscription()], {"pages_complete": False}),
                              ([self.subscription()], {"account_matches": 2}),
                              ([self.subscription(category="goods")], {})):
            with self.subTest(changes=changes), self.assertRaises(tariff.SubscriptionError):
                self.normalized(rows, **changes)
        for rows, state in (([], "account_not_returned"), ([self.client_row(subscription=None)], "subscription_missing")):
            self.success(self.response(rows))
            result = tariff.refresh(self.connection, self.owner)
            self.assertEqual(result["state"], state)
            shown = tariff.display_state(self.connection, self.owner)
            self.assertIsNone(shown["is_active"])
            self.assertEqual(shown["access_state"], "confirmed")

    def test_freshness_validation_rejects_missing_naive_future_or_old_received_time(self):
        for stamp in (None, "bad", timezone.now().replace(tzinfo=None),
                      timezone.now()+timedelta(minutes=1), timezone.now()-timedelta(minutes=11)):
            with self.subTest(stamp=stamp), self.assertRaisesMessage(tariff.SubscriptionError, "tariff_data_invalid"):
                self.normalized(fetched_at=stamp)

    def test_real_reader_success_preserves_manual_source_and_goods(self):
        self.manual()
        before = copy.deepcopy(self.saved()[calendar.KEY])
        self.success(self.response([self.client_row(subscription=self.subscription(expiredTo="2031-03-04T19:34:56+07:00"))]))
        result = tariff.refresh(self.connection, self.owner)
        self.assertEqual(result["code"], "")
        self.assertTrue(result["request_performed"])
        shown = tariff.display_state(self.connection, self.owner)
        self.assertEqual(shown["access_state"], "confirmed")
        self.assertEqual(shown["level"], "extended")
        self.assertEqual(shown["expires_at"].utcoffset(), timedelta(hours=7))
        self.assertEqual(self.saved()[calendar.KEY], before)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {"untouched": True})
        self.enforce_rate_mock.assert_called_with(tariff.ACCOUNT, "agency_clients_subscription", seconds=60)
        self._clients_page_mock.assert_called_once_with("synthetic-token", 0)

    def test_transport_has_fixed_body_official_path_and_no_redirect_or_agency_client_header(self):
        self._json_request_mock.side_effect = None
        self._json_request_mock.return_value = self.response([])
        self.real_clients_page("synthetic-token", 0)
        args, kwargs = self._json_request_mock.call_args
        self.assertEqual(args, ("https://api.avito.ru/api/1/agency/clients",))
        self.assertEqual(json.loads(kwargs["data"]), {"limit": 100, "offset": 0, "extra": {"subscription": True}})
        self.assertEqual(kwargs["method"], "POST")
        self.assertTrue(kwargs["ambiguous_transport"])
        self.assertNotIn("X-AgencyClientId", kwargs["headers"])
        with override_settings(AVITO_API_ROOT="https://untrusted.invalid"):
            result = tariff.refresh(self.connection, self.owner)
        self.assertEqual(result["code"], "tariff_read_failed")
        self.access_token_mock.assert_not_called()
        self._clients_page_mock.assert_not_called()

    def test_permission_refusal_preserves_previous_success_and_never_marks_expired(self):
        self.manual()
        self.store_success()
        before = copy.deepcopy(self.saved())
        result = tariff.refresh(self.connection, self.owner)
        self.assertEqual(result["code"], "provider_http_403")
        shown = tariff.display_state(self.connection, self.owner)
        self.assertEqual(shown["state"], "unavailable")
        self.assertEqual(shown["access_state"], "denied")
        self.assertEqual(shown["observed_state"], "active")
        after = self.saved()
        self.assertEqual(after[calendar.KEY], before[calendar.KEY])
        self.assertEqual(after[tariff.KEY]["last_success_at"], before[tariff.KEY]["last_success_at"])
        self.assertEqual(after[tariff.KEY]["data"], before[tariff.KEY]["data"])
        self.assertNotIn("expired", shown["state"])

    def test_authentication_error_and_method_403_are_different_access_evidence(self):
        self.access_token_mock.side_effect = AvitoError("provider_http_401")
        result = tariff.refresh(self.connection, self.owner)
        self.assertFalse(result["request_performed"])
        self.assertEqual(tariff.display_state(self.connection, self.owner)["access_state"], "authorization_error")
        self._clients_page_mock.assert_not_called()
        self.access_token_mock.side_effect = None
        self.access_token_mock.return_value = "synthetic-token"
        self.authorized_account_id_mock.return_value = "999"
        result = tariff.refresh(self.connection, self.owner)
        self.assertEqual(result["code"], "tariff_account_mismatch")
        self._clients_page_mock.assert_not_called()

    def test_pages_are_resumed_one_at_a_time_without_other_clients_or_early_success(self):
        first = [self.client_row(700000+i, subscription=None) for i in range(100)]
        self.success(self.response(first, total=101))
        result = tariff.refresh(self.connection, self.owner)
        self.assertEqual(result["code"], "tariff_pages_incomplete")
        state = self.saved()[tariff.KEY]
        self.assertEqual(state["progress"]["offset"], 100)
        self.assertEqual(len(state["progress"]["fingerprints"]), 100)
        self.assertNotIn("PRIVATE-SYNTHETIC", str(state))
        self.assertNotIn("700000", str(state))
        self.assertNotIn("last_success_at", state)
        self.assertFalse(tariff.setup_evidence(self.owner)["pagination_complete"])
        self.success(self.response([self.client_row()], total=101))
        result = tariff.refresh(self.connection, self.owner)
        self.assertEqual(result["state"], "active")
        self._clients_page_mock.assert_called_with("synthetic-token", 100)
        state = self.saved()[tariff.KEY]
        self.assertIsNone(state["progress"])
        self.assertEqual(state["pages_received"], 2)
        receipt = tariff.setup_evidence(self.owner)
        self.assertEqual(receipt["target_matches"], 1)
        self.assertNotIn("fingerprints", str(receipt))
        self.assertNotIn("level", receipt)
        self.assertNotIn("expires_at", receipt)

    def test_duplicate_overlap_changed_total_missing_fields_and_reader_cap_fail_closed(self):
        first = [self.client_row(800000+i, subscription=None) for i in range(100)]
        for second, expected in ((self.response([first[0]], total=101), "tariff_ambiguous"),
                (self.response([self.client_row()], total=102), "tariff_pages_incomplete")):
            self.connection.settings = {}
            self.connection.save(update_fields=["settings"])
            self.success(self.response(first, total=101))
            tariff.refresh(self.connection, self.owner)
            self.success(second)
            self.assertEqual(tariff.refresh(self.connection, self.owner)["code"], expected)
            self.assertNotIn("last_success_at", self.saved()[tariff.KEY])
        self.success(self.response(first, total=301))
        self.assertEqual(tariff.refresh(self.connection, self.owner)["code"], "tariff_reader_limit")
        self.assertEqual(tariff.display_state(self.connection, self.owner)["access_state"], "confirmed")
        self.assertFalse(tariff.setup_evidence(self.owner)["pagination_complete"])
        for response in ({"status": "error", "result": {"clients": [], "total": 0}},
                         self.response([{"clientId": tariff.ACCOUNT}]),
                         self.response([self.client_row()], total=True)):
            self.success(response)
            result = tariff.refresh(self.connection, self.owner)
            self.assertEqual(result["code"], "tariff_data_invalid")
            self.assertNotIn("PRIVATE-SYNTHETIC", str(result))

    def test_cooldown_preserves_cursor_and_does_not_perform_another_post(self):
        first = [self.client_row(900000+i, subscription=None) for i in range(100)]
        self.success(self.response(first, total=101))
        tariff.refresh(self.connection, self.owner)
        before = copy.deepcopy(self.saved()[tariff.KEY]["progress"])
        from pool_service.avito_workspace import AvitoCooldownError
        self.enforce_rate_mock.side_effect = AvitoCooldownError(timezone.now()+timedelta(minutes=1))
        self._clients_page_mock.reset_mock()
        result = tariff.refresh(self.connection, self.owner)
        self.assertEqual(result["code"], "provider_cooldown")
        self.assertFalse(result["request_performed"])
        self._clients_page_mock.assert_not_called()
        self.assertEqual(self.saved()[tariff.KEY]["progress"], before)

    def test_parallel_refresh_and_replaced_or_expired_lease_cannot_commit_twice(self):
        nested = []
        def during_read(*_args):
            nested.append(tariff.refresh(self.connection, self.owner))
            return self.response()
        self._clients_page_mock.side_effect = during_read
        result = tariff.refresh(self.connection, self.owner)
        self.assertEqual(result["code"], "")
        self.assertEqual(nested[0]["code"], "tariff_read_busy")
        self.assertEqual(self._clients_page_mock.call_count, 1)
        for replace_lease in (True, False):
            def superseded(*_args):
                metadata = copy.deepcopy(self.saved())
                if replace_lease:
                    metadata[tariff.KEY]["lease"] = "new-lease"
                else:
                    metadata[tariff.KEY]["lease_until"] = (timezone.now()-timedelta(seconds=1)).isoformat()
                self.connection.settings = metadata
                self.connection.save(update_fields=["settings"])
                return self.response()
            metadata = self.saved()
            metadata[tariff.KEY]["lease"] = None
            self.connection.settings = metadata
            self.connection.save(update_fields=["settings"])
            self._clients_page_mock.side_effect = superseded
            with self.subTest(replace_lease=replace_lease), self.assertRaisesMessage(tariff.SubscriptionError, "tariff_scope_changed"):
                tariff.refresh(self.connection, self.owner)

    def test_fresh_binding_and_rights_are_checked_again_after_http(self):
        def changed(*_args):
            credential = AvitoCredential.objects.get(pk=self.credential.pk)
            credential.client_secret_encrypted = "rotated-synthetic-secret"
            credential.save(update_fields=["client_secret_encrypted"])
            return self.response()
        self._clients_page_mock.side_effect = changed
        with self.assertRaisesMessage(tariff.SubscriptionError, "tariff_scope_changed"):
            tariff.refresh(self.connection, self.owner)
        self.assertIsNone(tariff.display_state(self.connection, self.owner)["last_success_at"])
        self._clients_page_mock.side_effect = lambda *_: (User.objects.filter(pk=self.owner.pk).update(is_active=False), self.response())[1]
        with self.assertRaises(PermissionDenied):
            tariff.refresh(self.connection, self.owner)
        self.assertNotIn("last_success_at", self.saved()[tariff.KEY])

    def test_category_mismatch_and_organization_change_never_become_inactive_or_cross_scope(self):
        self.success(self.response([self.client_row(subscription=self.subscription(category="goods", isActive=False))]))
        self.assertEqual(tariff.refresh(self.connection, self.owner)["code"], "tariff_category_mismatch")
        self.assertIsNone(tariff.display_state(self.connection, self.owner)["observed_state"])
        def changed_org(*_args):
            CommunicationChannel.objects.filter(pk=self.channel.pk).update(organization=self.other_org)
            return self.response()
        self._clients_page_mock.side_effect = changed_org
        with self.assertRaises(PermissionDenied):
            tariff.refresh(self.connection, self.owner)
        self.assertNotIn("last_success_at", self.saved()[tariff.KEY])
        self.assertEqual(tariff.display_state(self.connection, self.owner), {})

    def test_scope_manager_delegation_foreign_owner_goods_and_revocation_are_enforced(self):
        for connection, actor in ((self.connection, self.manager), (self.connection, self.foreign), (self.goods, self.owner)):
            with self.subTest(connection=connection.pk, actor=actor.pk), self.assertRaises(PermissionDenied):
                tariff.refresh(connection, actor)
        CommunicationAccess.objects.filter(user=self.manager, organization=self.org).update(can_manage_channels=True)
        result = tariff.refresh(self.connection, self.manager)
        self.assertEqual(result["code"], "provider_http_403")
        OrganizationAccess.objects.create(user=self.manager, organization=self.other_org, role="admin")
        CommunicationAccess.objects.filter(user=self.manager, organization=self.other_org).update(can_manage_channels=True)
        CommunicationAccess.objects.filter(user=self.manager, organization=self.org).update(can_manage_channels=False)
        with self.assertRaises(PermissionDenied):
            tariff.refresh(self.connection, self.manager)
        self.assertEqual(tariff.display_state(self.connection, self.manager), {})
        OrganizationAccess.objects.filter(user=self.owner, organization=self.org).update(role="manager")
        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(can_manage_channels=False)
        with self.assertRaises(PermissionDenied):
            tariff.refresh(self.connection, self.owner)

    def test_probe_is_claimed_before_http_and_not_repeated_after_403_or_unknown_result(self):
        from pool_service import avito_services_workplace_setup as setup
        for failure in (AvitoError("provider_http_403"), RuntimeError("PRIVATE-RAW")):
            self.connection.settings = {}
            self.connection.save(update_fields=["settings"])
            def denied(*_args):
                self.assertEqual(self.saved()[tariff.KEY]["probe_contract"], tariff.CONTRACT_VERSION)
                raise failure
            self._clients_page_mock.side_effect = denied
            self._clients_page_mock.reset_mock()
            with patch.object(setup, "_connection", return_value=self.connection), patch.object(setup, "_actor", return_value=self.owner):
                first = tariff.probe_once()
                second = tariff.probe_once()
            self.assertEqual(first["probe_state"], "attempted")
            self.assertEqual(second["probe_state"], "already_attempted")
            self.assertFalse(second["request_performed"])
            self.assertEqual(self._clients_page_mock.call_count, 1)
            self.assertNotIn("PRIVATE-RAW", str(first))

    def test_manual_and_api_dates_conflict_without_silent_replacement_and_stale_is_explicit(self):
        self.manual()
        before = copy.deepcopy(self.saved()[calendar.KEY])
        date = timezone.localdate(timezone=tariff.ZONE)+timedelta(days=31)
        self.store_success(self.normalized([self.subscription(expiredTo=date.isoformat()+"T03:00:00Z")]))
        shown = tariff.display_state(self.connection, self.owner)
        self.assertTrue(shown["dates_comparable"])
        self.assertTrue(shown["dates_conflict"])
        self.assertTrue(shown["manual_source"])
        self.assertEqual(self.saved()[calendar.KEY], before)
        metadata = self.saved()
        metadata[tariff.KEY]["last_success_at"] = (timezone.now()-timedelta(days=3)).isoformat()
        metadata[tariff.KEY]["last_attempt_code"] = "provider_http_403"
        self.connection.settings = metadata
        self.connection.save(update_fields=["settings"])
        shown = tariff.display_state(self.connection, self.owner)
        self.assertTrue(shown["stale"])
        self.assertEqual(shown["observed_state"], "active")
        self.assertEqual(shown["state"], "unavailable")

    def test_corrupt_cached_fields_and_old_contract_blocker_do_not_leak_or_claim_unknown_format(self):
        self.store_success()
        original = copy.deepcopy(self.saved())
        for field, raw in (("state", ["PRIVATE"]), ("level", {"PRIVATE": True}), ("expires_at", "PRIVATE")):
            metadata = copy.deepcopy(original)
            metadata[tariff.KEY]["data"][field] = raw
            self.connection.settings = metadata
            self.connection.save(update_fields=["settings"])
            shown = tariff.display_state(self.connection, self.owner)
            self.assertEqual(shown["code"], "tariff_data_invalid")
            self.assertNotIn("PRIVATE", str(shown))
            self.assertIsNone(shown["last_success_at"])
        metadata = copy.deepcopy(original)
        metadata[tariff.KEY]["last_attempt_code"] = "method_contract_unavailable"
        self.connection.settings = metadata
        self.connection.save(update_fields=["settings"])
        self.assertEqual(tariff.display_state(self.connection, self.owner)["code"], "access_unverified")
        self.assertEqual(self.saved()[tariff.KEY]["last_attempt_code"], "method_contract_unavailable")

    def test_bad_progress_missing_credentials_and_csrf_fail_without_network(self):
        current, binding = tariff._scope(4, self.owner)
        self.connection.settings = {tariff.KEY: {"binding": binding, "progress": {
            "started_at": timezone.now().isoformat(), "PRIVATE": "discard"}}}
        self.connection.save(update_fields=["settings"])
        self.assertEqual(tariff.refresh(self.connection, self.owner)["code"], "tariff_data_invalid")
        self._clients_page_mock.assert_not_called()
        self.credential.delete()
        self.assertEqual(tariff.refresh(self.connection, self.owner)["code"], "provider_credentials_missing")
        self._clients_page_mock.assert_not_called()
        strict = TestClient(enforce_csrf_checks=True)
        strict.force_login(self.owner)
        url = reverse("avito_refresh_data", args=[4])
        self.assertEqual(strict.post(url, {"section": "tariff_api_refresh"}).status_code, 403)

    def test_record_observation_rejects_wrong_binding_contract_and_inconsistent_state(self):
        current, binding = tariff._scope(4, self.owner)
        for observation, source in ((replace(self.normalized(), is_active=None), binding),
                (replace(self.normalized(), expiry_state="date_unconfirmed"), binding),
                (self.normalized(), {**binding, "organization_id": self.other_org.pk})):
            with self.assertRaises(tariff.SubscriptionError):
                tariff.record_observation(current, self.owner, observation, source)
        with patch.object(tariff, "CONTRACT_STATUS", "unverified"):
            with self.assertRaisesMessage(tariff.SubscriptionError, "method_contract_unavailable"):
                tariff.refresh(self.connection, self.owner)
        self._clients_page_mock.assert_not_called()


@override_settings(AVITO_API_ROOT="https://api.avito.ru")
class TariffTransportTransactionTests(TransactionTestCase):
    def test_new_http_read_is_outside_application_transaction(self):
        org = Organization.objects.create(name="Synthetic transaction organization",
            paid_until=timezone.now()+timedelta(days=365))
        owner = User.objects.create_user("tariff-transaction-owner")
        OrganizationAccess.objects.create(organization=org, user=owner, role="owner")
        channel = CommunicationChannel.objects.create(organization=org, kind="avito", name="Services")
        connection = ChannelConnection.objects.create(pk=4, channel=channel, external_id=tariff.ACCOUNT)
        AvitoCredential.objects.create(connection=connection, client_id_encrypted="synthetic-id",
            client_secret_encrypted="synthetic-secret")
        def read(*_args):
            self.assertFalse(database.in_atomic_block)
            return {"status": "ok", "result": {"clients": [], "total": 0}}
        with patch.object(tariff, "access_token", return_value="synthetic-token"), \
             patch.object(tariff, "authorized_account_id", return_value=tariff.ACCOUNT), \
             patch.object(tariff, "enforce_rate"), patch.object(tariff, "_clients_page", side_effect=read):
            self.assertEqual(tariff.refresh(connection, owner)["state"], "account_not_returned")
