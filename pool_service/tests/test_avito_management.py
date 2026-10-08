from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from pool_service.communication_avito import AvitoError
from pool_service.communication_models import AvitoCredential, ChannelConnection, CommunicationChannel
from pool_service.models import Organization, OrganizationAccess


class AvitoManagementTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Test Avito management")
        self.owner = User.objects.create_user("owner-avito", password="test")
        self.manager = User.objects.create_user("manager-avito", password="test")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role="manager")
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Авито")
        self.connection = ChannelConnection.objects.create(
            channel=self.channel, name="Товары", external_id="123"
        )
        AvitoCredential.objects.create(
            connection=self.connection,
            client_id_encrypted="encrypted-not-plaintext",
            client_secret_encrypted="encrypted-not-plaintext",
        )
        self.url = reverse("avito_dashboard")
        self.check_url = reverse("avito_check_api", args=[self.connection.pk])
        self.settings_url = reverse("communication_connection_edit", args=[self.connection.pk])

    def test_page_is_scoped_to_channel_managers_and_does_not_call_api_on_get(self):
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.check_url).status_code, 403)
        self.client.force_login(self.owner)
        with patch("pool_service.avito_management.access_token") as get_token:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Товары")
        self.assertNotContains(response, "Проверить API")
        self.assertContains(self.client.get(self.settings_url), "Проверить API")
        get_token.assert_not_called()
        self.assertEqual(self.client.get(self.check_url).status_code, 405)
        self.assertIn("no-store", response.get("Cache-Control", ""))

    @patch("pool_service.avito_management.webhook_subscriptions", return_value=[])
    @patch("pool_service.avito_management._json_request", return_value={})
    @patch("pool_service.avito_management.verify_messenger_access", return_value=True)
    @patch("pool_service.avito_management.authorized_account_id", return_value="987")
    @patch("pool_service.avito_management.access_token", return_value="opaque-access-token")
    def test_explicit_check_reports_capabilities_and_id_mismatch(
        self, _token, _account, _messenger, api_request, _subscriptions
    ):
        self.client.force_login(self.owner)
        response = self.client.post(self.check_url)
        self.assertRedirects(response, self.settings_url + "#avito-api-diagnostics")
        self.assertEqual(api_request.call_count, 3)
        self.connection.refresh_from_db()
        saved = self.connection.settings
        self.assertEqual(saved["avito_api_account_id"], "987")
        self.assertEqual(len(saved["avito_api_results"]), 7)
        self.assertEqual(saved["avito_api_results"][1]["status"], "warning")
        self.assertEqual(saved["avito_api_results"][2]["status"], "ok")
        self.assertEqual(saved["avito_api_results"][3]["status"], "warning")
        self.assertNotIn("opaque-access-token", str(saved))
        self.assertContains(self.client.get(self.settings_url), "отличается от сохранённого")

    @patch("pool_service.avito_management.webhook_subscriptions", return_value=[])
    @patch("pool_service.avito_management._json_request", return_value={})
    @patch("pool_service.avito_management.verify_messenger_access",
           side_effect=AvitoError("provider_http_403"))
    @patch("pool_service.avito_management.authorized_account_id", return_value="123")
    @patch("pool_service.avito_management.access_token", return_value="opaque-token")
    def test_messenger_denial_does_not_hide_other_available_api(
        self, _token, _account, _messenger, _other_api, _subscriptions
    ):
        self.client.force_login(self.owner)
        self.client.post(self.check_url)
        results = {row["key"]: row for row in ChannelConnection.objects.get(pk=self.connection.pk).settings["avito_api_results"]}
        self.assertEqual(results["messenger"]["status"], "denied")
        self.assertEqual(results["messenger"]["code"], "provider_http_403")
        self.assertEqual(results["items"]["status"], "ok")
        self.assertEqual(results["autoload"]["status"], "ok")

    @patch("pool_service.avito_management.access_token",
           side_effect=AvitoError("provider_http_401"))
    def test_bad_credentials_skip_other_checks(self, _token):
        self.client.force_login(self.owner)
        self.client.post(self.check_url)
        results = ChannelConnection.objects.get(pk=self.connection.pk).settings["avito_api_results"]
        self.assertEqual(results[0]["status"], "denied")
        self.assertTrue(all(row["status"] == "skipped" for row in results[1:]))

    @patch("pool_service.avito_management.access_token",
           side_effect=AvitoError("unexpected PRIVATE PAYLOAD 12345"))
    def test_unexpected_error_is_not_saved(self, _token):
        self.client.force_login(self.owner)
        self.client.post(self.check_url)
        settings = ChannelConnection.objects.get(pk=self.connection.pk).settings
        self.assertNotIn("PRIVATE", str(settings))
        self.assertEqual(settings["avito_api_results"][0]["code"], "provider_error")

    def test_foreign_organization_is_not_visible_or_probeable(self):
        other_org = Organization.objects.create(name="Unrelated")
        foreign_channel = CommunicationChannel.objects.create(organization=other_org, kind="avito", name="Other")
        foreign_connection = ChannelConnection.objects.create(
            channel=foreign_channel, name="Other account", external_id="222"
        )
        self.client.force_login(self.owner)
        self.assertNotContains(self.client.get(self.url), "Other account")
        self.assertEqual(
            self.client.post(reverse("avito_check_api", args=[foreign_connection.pk])).status_code,
            404,
        )

    def test_post_requires_csrf_token(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.owner)
        response = csrf_client.post(self.check_url)
        self.assertEqual(response.status_code, 403)
