import json

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from pool_service.onec_diagnostic_mcp_views import MCP_PROTOCOL_VERSION


@override_settings(
    ADVISOR_ONEC_DIAGNOSTIC_MCP_ENABLED=True,
    SITE_URL="https://rovikpool.ru",
    ADVISOR_ONEC_DIAGNOSTIC_MCP_RESOURCE_URL="https://service2.aqualine22.ru/mcp/1c",
    ADVISOR_ONEC_DIAGNOSTIC_MCP_AUTH_ISSUER="https://service2.aqualine22.ru/onec-diagnostic",
    ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_ORIGINS={"https://chatgpt.com"},
)
class OneCDiagnosticMcpChatGptDiscoveryTests(SimpleTestCase):
    def test_initialize_without_bearer_starts_oauth_like_finance_mcp(self):
        response = self.client.post(
            reverse("onec_diagnostic_mcp"),
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "ChatGPT", "version": "test"},
                },
            }),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            HTTP_ORIGIN="https://chatgpt.com",
        )
        self.assertEqual(response.status_code, 401)
        challenge = response["WWW-Authenticate"]
        self.assertIn(
            'resource_metadata="https://service2.aqualine22.ru/.well-known/oauth-protected-resource/mcp/1c"',
            challenge,
        )
        self.assertIn('scope="onec.diagnostic.read"', challenge)

    def test_tools_list_without_bearer_starts_oauth_like_finance_mcp(self):
        response = self.client.post(
            reverse("onec_diagnostic_mcp"),
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            HTTP_ORIGIN="https://chatgpt.com",
            HTTP_MCP_PROTOCOL_VERSION=MCP_PROTOCOL_VERSION,
        )
        self.assertEqual(response.status_code, 401)
        challenge = response["WWW-Authenticate"]
        self.assertIn(
            'resource_metadata="https://service2.aqualine22.ru/.well-known/oauth-protected-resource/mcp/1c"',
            challenge,
        )

    def test_authorization_server_metadata_does_not_leak_site_url_host(self):
        response = self.client.get(
            reverse("onec_diagnostic_mcp_authorization_server_metadata")
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(
            data["authorization_endpoint"],
            "https://service2.aqualine22.ru/oauth/1c/authorize",
        )
        self.assertEqual(
            data["token_endpoint"],
            "https://service2.aqualine22.ru/oauth/1c/token",
        )
        self.assertNotIn("rovikpool.ru", json.dumps(data))
