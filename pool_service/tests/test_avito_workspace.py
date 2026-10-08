from datetime import timedelta
from unittest.mock import patch

from django.test import Client, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service import avito_workspace
from pool_service.communication_avito import AvitoError
from pool_service.communication_models import (
    ChannelConnection, CommunicationAccess, Conversation, ConversationMessage,
)
from pool_service.tests.test_avito_management import AvitoManagementTests


class AvitoWorkspaceParsingTests(SimpleTestCase):
    def test_items_keep_only_allowlisted_data_and_reject_unsafe_urls(self):
        result = avito_workspace.items_data({"resources": [{
            "id": 91, "title": "Pool", "status": "active", "price": 0,
            "category": {"name": "Goods"}, "url": "javascript:alert(1)",
            "access_token": "PRIVATE", "seller_phone": "PRIVATE",
        }], "meta": {"total": 1}}, page=1, status="")
        self.assertEqual(result["rows"][0]["price"], "0.00")
        self.assertEqual(result["rows"][0]["url"], "")
        self.assertNotIn("PRIVATE", str(result))
        self.assertFalse(result["has_next"])
        self.assertFalse(result["has_previous"])

    def test_empty_and_missing_items_are_different(self):
        self.assertEqual(avito_workspace.items_data({"resources": []}, page=1, status="")["rows"], [])
        for payload in ({}, {"resources": {}}, {"resources": [None]}, {"resources": [{"id": True}]}, {"resources": [{"id": "1" * 33}]}):
            with self.assertRaises(AvitoError):
                avito_workspace.items_data(payload, page=1, status="")

    def test_pagination_does_not_invent_total(self):
        result = avito_workspace.items_data({"resources": [{"id": x + 1} for x in range(50)]}, page=2, status="active")
        self.assertIsNone(result["total"])
        self.assertTrue(result["has_next"])
        self.assertTrue(result["has_previous"])
        self.assertEqual(result["page"], 2)
        self.assertEqual(result["status"], "active")

    def test_balance_missing_values_are_not_zero(self):
        self.assertEqual(avito_workspace.balance_data({"real": 0}), {"real": "0.00", "bonus": None, "currency": "RUB"})
        for payload in ({}, {"real": float("nan")}, {"real": True}, {"real": "Infinity"}):
            with self.assertRaises(AvitoError):
                avito_workspace.balance_data(payload)

    def test_profile_excludes_private_fields_and_url_queries(self):
        result = avito_workspace.profile_data({"id": 123, "name": "Test", "email": "PRIVATE",
            "phone": "PRIVATE", "profile_url": "https://www.avito.ru/user/123?secret=PRIVATE"})
        self.assertNotIn("PRIVATE", str(result))
        self.assertEqual(result["profile_url"], "https://www.avito.ru/user/123")

    def test_autoload_unknown_payload_is_not_success(self):
        with self.assertRaises(AvitoError):
            avito_workspace.autoload_data({"unknown": "private data"})
        result = avito_workspace.autoload_data({"upload": {"id": "u1", "status": "success",
            "statistics": {"total": 10, "errors": 0, "secret": "PRIVATE"}, "private": "PRIVATE"}})
        self.assertEqual(result["counters"][1], {"label": "Ошибок", "value": 0})
        self.assertNotIn("PRIVATE", str(result))

    def test_allowed_url_hosts_are_exact(self):
        for url in ("https://avito.ru.evil.test/x", "https://user:pass@avito.ru/x", "http://avito.ru/x", "https://avito.ru:444/x"):
            self.assertEqual(avito_workspace._avito_url(url), "")

    @patch("pool_service.avito_workspace.enforce_rate")
    @patch("pool_service.avito_workspace._json_request", return_value={"resources": []})
    def test_item_request_is_get_with_bounded_page_size(self, fetch, _rate):
        avito_workspace.fetch_section("items", "opaque", "123", page=2, status="active")
        args, kwargs = fetch.call_args
        self.assertIn("per_page=50&page=2&status=active", args[0])
        self.assertNotIn("method", kwargs)
        self.assertEqual(kwargs["timeout"], 8)


class AvitoWorkspaceViewTests(TestCase):
    def setUp(self):
        AvitoManagementTests.setUp(self)
        limiter = patch("pool_service.avito_workspace.enforce_rate")
        limiter.start()
        self.addCleanup(limiter.stop)

    def refresh_url(self, connection=None):
        return reverse("avito_refresh_data", args=[(connection or self.connection).pk])

    def report(self):
        self.connection.refresh_from_db()
        return self.connection.settings["avito_workspace"]["sections"]

    @staticmethod
    def provider(token, path):
        if path == "/core/v1/accounts/self":
            return {"id": 123, "name": "Synthetic profile"}
        if "balance" in path:
            return {"real": 125, "bonus": 0}
        if "/items?" in path:
            return {"resources": [{"id": 11, "title": "Synthetic item", "status": "active", "price": 0}], "meta": {"total": 1}}
        if "last_successful" in path:
            return {"id": "upload-1", "status": "success", "statistics": {"total": 1}}
        raise AssertionError(path)

    @patch("pool_service.avito_management.access_token", return_value="PRIVATE_TOKEN")
    @patch("pool_service.avito_workspace._get", side_effect=provider)
    def test_refresh_all_saves_safe_real_data_and_get_does_not_call_provider(self, fetch, token):
        self.client.force_login(self.owner)
        response = self.client.post(self.refresh_url(), {"section": "all"})
        self.assertRedirects(response, self.url + f"?account={self.connection.pk}")
        self.assertEqual(fetch.call_count, 4)
        report = self.report()
        self.assertEqual(report["balance"]["data"]["real"], "125.00")
        self.assertEqual(report["items"]["data"]["rows"][0]["title"], "Synthetic item")
        self.assertNotIn("PRIVATE_TOKEN", str(self.connection.settings))
        self.assertNotIn("avito_workspace_refresh_lease", self.connection.settings)
        fetch.reset_mock()
        token.reset_mock()
        self.assertEqual(self.client.get(self.url).status_code, 200)
        fetch.assert_not_called()
        token.assert_not_called()

    @patch("pool_service.avito_management.access_token", return_value="opaque")
    @patch("pool_service.avito_workspace._get", side_effect=provider)
    def test_error_keeps_original_snapshot_and_query(self, fetch, _token):
        self.client.force_login(self.owner)
        self.client.post(self.refresh_url(), {"section": "items", "page": 1, "status": "active"})
        original = self.report()["items"]
        fetch.side_effect = lambda token, path: {"id": 123} if "accounts/self" in path else (_ for _ in ()).throw(AvitoError("provider_http_403"))
        self.client.post(self.refresh_url(), {"section": "items", "page": 2, "status": "old"})
        actual = self.report()["items"]
        self.assertEqual(actual["data"], original["data"])
        self.assertEqual(actual["success_at"], original["success_at"])
        self.assertEqual(actual["status"], "denied")
        self.assertTrue(actual["stale"])

    @patch("pool_service.avito_management.access_token", return_value="opaque")
    @patch("pool_service.avito_workspace._get", return_value={"id": 999})
    def test_mismatched_identity_prevents_other_requests(self, fetch, _token):
        self.client.force_login(self.owner)
        self.client.post(self.refresh_url(), {"section": "all"})
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(all(row["code"] == "provider_account_mismatch" for row in self.report().values()))
        self.assertTrue(all("data" not in row for row in self.report().values()))

    @patch("pool_service.avito_management.access_token")
    def test_invalid_query_and_active_lease_do_not_call_api(self, token):
        self.client.force_login(self.owner)
        for data in ({"section": "delete"}, {"section": "items", "page": -1}, {"section": "items", "status": "evil"}):
            self.client.post(self.refresh_url(), data)
        self.connection.settings = {"avito_workspace_refresh_until": (timezone.now() + timedelta(minutes=1)).isoformat()}
        self.connection.save(update_fields=["settings"])
        self.client.post(self.refresh_url(), {"section": "all"})
        token.assert_not_called()

    @patch("pool_service.avito_management.access_token", side_effect=AvitoError("PRIVATE PAYLOAD"))
    def test_failed_authorization_is_sanitized_and_lease_released(self, _token):
        self.client.force_login(self.owner)
        self.client.post(self.refresh_url(), {"section": "balance"})
        self.assertEqual(self.report()["balance"]["code"], "provider_error")
        self.assertNotIn("PRIVATE", str(self.connection.settings))
        self.assertNotIn("avito_workspace_refresh_lease", self.connection.settings)

    def test_refresh_permission_csrf_and_get_boundaries(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(self.refresh_url(), {"section": "all"}).status_code, 403)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(self.refresh_url()).status_code, 405)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.owner)
        self.assertEqual(csrf.post(self.refresh_url(), {"section": "all"}).status_code, 403)
        self.assertEqual(self.client.get(self.url + "?account=987654").status_code, 404)

    def test_foreign_connection_cannot_be_read_or_refreshed(self):
        from pool_service.models import Organization
        from pool_service.communication_models import CommunicationChannel
        foreign_org = Organization.objects.create(name="Foreign")
        channel = CommunicationChannel.objects.create(organization=foreign_org, kind="avito", name="Foreign")
        connection = ChannelConnection.objects.create(channel=channel, name="Foreign", external_id="555")
        self.client.force_login(self.owner)
        self.assertEqual(self.client.post(self.refresh_url(connection), {"section": "all"}).status_code, 404)
        self.assertEqual(self.client.get(self.url + f"?account={connection.pk}").status_code, 404)

    @patch("pool_service.avito_management.access_token", return_value="opaque")
    @patch("pool_service.avito_workspace._get")
    def test_concurrent_account_edit_cannot_save_wrong_identity(self, fetch, _token):
        def change_id(token, path):
            ChannelConnection.objects.filter(pk=self.connection.pk).update(external_id="456")
            return {"id": 123}
        fetch.side_effect = change_id
        self.client.force_login(self.owner)
        response = self.client.post(self.refresh_url(), {"section": "profile"}, follow=True)
        self.connection.refresh_from_db()
        self.assertNotIn("avito_workspace", self.connection.settings)
        self.assertNotContains(response, "Данные Авито обновлены.")

    def test_local_analytics_are_scoped_and_exclude_unsent_messages(self):
        conversation = Conversation.objects.create(organization=self.org, connection=self.connection, external_id="chat1", participant_name="Synthetic")
        ConversationMessage.objects.create(conversation=conversation, direction="in")
        ConversationMessage.objects.create(conversation=conversation, direction="out", delivery_status="pending")
        ConversationMessage.objects.create(conversation=conversation, direction="out", delivery_status="delivered")
        self.client.force_login(self.owner)
        data = self.client.get(self.url).context["local_analytics"]
        self.assertEqual((data["conversations"], data["inbound"], data["outbound"]), (1, 1, 1))
        self.assertTrue(data["available"])
        self.assertTrue(data["timezone"])
        CommunicationAccess.objects.filter(user=self.owner, organization=self.org).update(can_view_conversations=False)
        data = self.client.get(self.url).context["local_analytics"]
        self.assertFalse(data["available"])
        self.assertEqual(data["inbound"], 0)

    def test_invalid_date_range_and_malformed_saved_metadata_do_not_crash(self):
        self.connection.settings = {"avito_workspace": {"account_id": "123", "sections": "wrong"}}
        self.connection.save(update_fields=["settings"])
        self.client.force_login(self.owner)
        response = self.client.get(self.url + "?date_from=2030-01-01&date_to=2020-01-01")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["local_analytics"]["period_error"])
        self.assertEqual(response.context["workspace"], {})

    @patch("pool_service.avito_management.access_token", return_value="opaque")
    @patch("pool_service.avito_workspace._get", side_effect=provider)
    def test_partial_failure_does_not_hide_successful_sections(self, fetch, _token):
        def partial(token, path):
            if "balance" in path:
                raise AvitoError("provider_http_429")
            return self.provider(token, path)
        fetch.side_effect = partial
        self.client.force_login(self.owner)
        self.client.post(self.refresh_url(), {"section": "all"})
        report = self.report()
        self.assertEqual(report["balance"]["status"], "warning")
        self.assertEqual(report["items"]["status"], "ok")
        self.assertEqual(report["autoload"]["status"], "ok")
        self.assertNotIn("data", report["balance"])

    @patch("pool_service.avito_management.access_token", return_value="opaque")
    @patch("pool_service.avito_workspace._get", side_effect=provider)
    def test_expired_lease_recovers_and_unrelated_settings_are_preserved(self, _fetch, _token):
        self.connection.settings = {"avito_workspace_refresh_until": (timezone.now() - timedelta(seconds=1)).isoformat(),
                                    "avito_webhook_status": "connected"}
        self.connection.save(update_fields=["settings"])
        self.client.force_login(self.owner)
        self.client.post(self.refresh_url(), {"section": "profile"})
        self.assertEqual(self.report()["profile"]["status"], "ok")
        self.assertEqual(self.connection.settings["avito_webhook_status"], "connected")

    @patch("pool_service.avito_management.access_token", return_value="opaque")
    @patch("pool_service.avito_workspace._get", side_effect=provider)
    def test_template_renders_zero_balance_and_escapes_provider_text(self, fetch, _token):
        def unsafe_text(token, path):
            result = self.provider(token, path)
            if "/items?" in path:
                result["resources"][0]["title"] = '<script>alert("xss")</script>'
            return result
        fetch.side_effect = unsafe_text
        self.client.force_login(self.owner)
        self.client.post(self.refresh_url(), {"section": "all"})
        response = self.client.get(self.url)
        self.assertContains(response, "0.00")
        self.assertNotContains(response, '<script>alert("xss")</script>')
        self.assertContains(response, "&lt;script&gt;")

    @patch("pool_service.avito_management.access_token", return_value="opaque")
    @patch("pool_service.avito_workspace._get")
    def test_concurrent_organization_move_discards_snapshot(self, fetch, _token):
        from pool_service.models import Organization
        from pool_service.communication_models import CommunicationChannel
        foreign = CommunicationChannel.objects.create(
            organization=Organization.objects.create(name="Foreign move"), kind="avito", name="Foreign",
        )
        def move_connection(token, path):
            ChannelConnection.objects.filter(pk=self.connection.pk).update(channel=foreign)
            return {"id": 123}
        fetch.side_effect = move_connection
        self.client.force_login(self.owner)
        self.client.post(self.refresh_url(), {"section": "profile"})
        self.connection.refresh_from_db()
        self.assertNotIn("avito_workspace", self.connection.settings)
