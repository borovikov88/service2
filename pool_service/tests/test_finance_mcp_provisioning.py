from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from pool_service.finance_mcp_auth import (
    CHATGPT_CLIENT_ID_METADATA_URL,
    FinanceMcpConfigurationError,
)
from pool_service.models import (
    FinanceMcpClient,
    FinanceMcpPrincipal,
    Organization,
    OrganizationAccess,
)


class FinanceMcpProvisioningFallbackTests(TestCase):
    redirect_uri = "https://chatgpt.com/connector_platform_oauth_redirect"

    def setUp(self):
        self.owner = User.objects.create_user("finance-mcp-provision-owner", password="pass")
        self.organization = Organization.objects.create(name="Provisioning org")
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.organization,
            role="owner",
        )

    def _call_provision(self, subject):
        call_command(
            "manage_finance_mcp_access",
            "provision",
            "--client-id", CHATGPT_CLIENT_ID_METADATA_URL,
            "--client-name", "ChatGPT finance adviser",
            "--principal-subject", subject,
            "--principal-name", "Finance adviser",
            "--redirect-uri", self.redirect_uri,
            "--organization-id", str(self.organization.id),
            "--actor-id", str(self.owner.id),
            "--confirm", CHATGPT_CLIENT_ID_METADATA_URL,
        )

    def test_network_block_uses_reviewed_pinned_metadata_snapshot(self):
        with patch(
            "pool_service.management.commands.manage_finance_mcp_access.fetch_trusted_chatgpt_client_metadata",
            side_effect=FinanceMcpConfigurationError(
                "Не удалось получить доверенный ChatGPT client metadata document."
            ),
        ):
            self._call_provision("chatgpt:finance:fallback-test")

        client = FinanceMcpClient.objects.get(client_id=CHATGPT_CLIENT_ID_METADATA_URL)
        self.assertEqual(client.redirect_uris, [self.redirect_uri])
        self.assertRegex(client.client_metadata_sha256, r"^[0-9a-f]{64}$")
        self.assertIsNotNone(client.client_metadata_verified_at)
        self.assertTrue(
            FinanceMcpPrincipal.objects.filter(
                subject="chatgpt:finance:fallback-test",
                organization_scopes__organization=self.organization,
            ).exists()
        )

    def test_reachable_but_invalid_metadata_still_fails_closed(self):
        with patch(
            "pool_service.management.commands.manage_finance_mcp_access.fetch_trusted_chatgpt_client_metadata",
            side_effect=FinanceMcpConfigurationError(
                "ChatGPT client metadata document не поддерживает public-client authorization code flow."
            ),
        ):
            with self.assertRaisesMessage(
                CommandError,
                "не поддерживает public-client authorization code flow",
            ):
                self._call_provision("chatgpt:finance:invalid-test")

        self.assertFalse(FinanceMcpClient.objects.exists())
        self.assertFalse(
            FinanceMcpPrincipal.objects.filter(subject="chatgpt:finance:invalid-test").exists()
        )
