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
    def _post(self, payload):
        return self.client.post(
            reverse("onec_diagnostic_mcp"),
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            HTTP_ORIGIN="https://chatgpt.com",
            HTTP_MCP_PROTOCOL_VERSION=MCP_PROTOCOL_VERSION,
        )

    def test_initialize_is_available_before_oauth(self):
        response = self._post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ChatGPT", "version": "test"},
            },
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"]["protocolVersion"], MCP_PROTOCOL_VERSION)

    def test_tools_list_is_available_before_oauth_and_declares_scope(self):
        response = self._post({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        })
        self.assertEqual(response.status_code, 200)
        tools = response.json()["result"]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["list_1c_entities", "get_1c_entity_schema", "read_1c_rows"],
        )
        for tool in tools:
            self.assertEqual(
                tool["securitySchemes"],
                [{"type": "oauth2", "scopes": ["onec.diagnostic.read"]}],
            )

    def test_unauthenticated_tool_call_returns_chatgpt_oauth_challenge(self):
        response = self._post({
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_1c_entities", "arguments": {}},
        })
        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertTrue(result["isError"])
        challenge = result["_meta"]["mcp/www_authenticate"][0]
        self.assertIn(
            'resource_metadata="https://service2.aqualine22.ru/.well-known/oauth-protected-resource/mcp/1c"',
            challenge,
        )
        self.assertIn('scope="onec.diagnostic.read"', challenge)

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
