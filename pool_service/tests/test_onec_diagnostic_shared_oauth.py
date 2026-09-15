import json
from unittest.mock import patch

from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from pool_service.finance_mcp_auth import FINANCE_READ_SCOPE, OFFLINE_ACCESS_SCOPE
from pool_service.onec_diagnostic_mcp_auth import DIAGNOSTIC_READ_SCOPE
from pool_service.onec_diagnostic_mcp_views import MCP_PROTOCOL_VERSION


ROOT = "https://service2.example.test"
DIAGNOSTIC_RESOURCE = f"{ROOT}/mcp/1c"
FINANCE_RESOURCE = f"{ROOT}/mcp/finance"


@override_settings(
    ADVISOR_FINANCE_MCP_ENABLED=True,
    ADVISOR_FINANCE_MCP_RESOURCE_URL=FINANCE_RESOURCE,
    ADVISOR_FINANCE_MCP_AUTH_ISSUER=ROOT,
    ADVISOR_FINANCE_MCP_ALLOWED_ORIGINS={"https://chatgpt.com"},
    ADVISOR_ONEC_DIAGNOSTIC_MCP_ENABLED=True,
    ADVISOR_ONEC_DIAGNOSTIC_MCP_RESOURCE_URL=DIAGNOSTIC_RESOURCE,
    # Deliberately stale: public Diagnostic discovery must ignore this old
    # path-based issuer and bind to the proven Finance authorization server.
    ADVISOR_ONEC_DIAGNOSTIC_MCP_AUTH_ISSUER=f"{ROOT}/onec-diagnostic",
    ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_ORIGINS={"https://chatgpt.com"},
)
class OneCDiagnosticSharedOAuthTests(SimpleTestCase):
    def test_diagnostic_resource_uses_finance_root_authorization_server(self):
        protected = self.client.get(
            reverse("onec_diagnostic_mcp_protected_resource_metadata")
        )
        self.assertEqual(protected.status_code, 200)
        self.assertEqual(protected.json(), {
            "resource": DIAGNOSTIC_RESOURCE,
            "authorization_servers": [ROOT],
            "scopes_supported": [DIAGNOSTIC_READ_SCOPE],
            "bearer_methods_supported": ["header"],
        })

    def test_root_authorization_metadata_advertises_both_resources_scopes(self):
        response = self.client.get(reverse("finance_mcp_authorization_server_metadata"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["issuer"], ROOT)
        self.assertEqual(data["authorization_endpoint"], f"{ROOT}/oauth/finance/authorize")
        self.assertEqual(data["token_endpoint"], f"{ROOT}/oauth/finance/token")
        self.assertEqual(
            set(data["scopes_supported"]),
            {FINANCE_READ_SCOPE, OFFLINE_ACCESS_SCOPE, DIAGNOSTIC_READ_SCOPE},
        )

    def test_old_diagnostic_metadata_alias_stays_backward_compatible(self):
        legacy = self.client.get(
            reverse("onec_diagnostic_mcp_authorization_server_metadata")
        )
        self.assertEqual(legacy.status_code, 200)
        data = legacy.json()
        self.assertEqual(data["authorization_endpoint"], f"{ROOT}/oauth/1c/authorize")
        self.assertEqual(data["token_endpoint"], f"{ROOT}/oauth/1c/token")
        self.assertIn(DIAGNOSTIC_READ_SCOPE, data["scopes_supported"])

    def test_shared_authorize_dispatches_by_exact_resource(self):
        with patch(
            "pool_service.mcp_oauth_shared.diagnostic_views.onec_diagnostic_oauth_authorize",
            return_value=HttpResponse("diagnostic"),
        ) as diagnostic, patch(
            "pool_service.mcp_oauth_shared.finance_mcp_views.finance_oauth_authorize",
            return_value=HttpResponse("finance"),
        ) as finance:
            diagnostic_response = self.client.get(
                reverse("finance_mcp_authorize"),
                {"resource": DIAGNOSTIC_RESOURCE},
            )
            finance_response = self.client.get(
                reverse("finance_mcp_authorize"),
                {"resource": FINANCE_RESOURCE},
            )
        self.assertEqual(diagnostic_response.content, b"diagnostic")
        self.assertEqual(finance_response.content, b"finance")
        diagnostic.assert_called_once()
        finance.assert_called_once()

    def test_shared_token_dispatches_by_exact_resource(self):
        with patch(
            "pool_service.mcp_oauth_shared.diagnostic_views.onec_diagnostic_oauth_token",
            return_value=HttpResponse("diagnostic"),
        ) as diagnostic, patch(
            "pool_service.mcp_oauth_shared.finance_mcp_views.finance_oauth_token",
            return_value=HttpResponse("finance"),
        ) as finance:
            diagnostic_response = self.client.post(
                reverse("finance_mcp_token"),
                {"resource": DIAGNOSTIC_RESOURCE},
            )
            finance_response = self.client.post(
                reverse("finance_mcp_token"),
                {"resource": FINANCE_RESOURCE},
            )
        self.assertEqual(diagnostic_response.content, b"diagnostic")
        self.assertEqual(finance_response.content, b"finance")
        diagnostic.assert_called_once()
        finance.assert_called_once()

    def test_chatgpt_tool_challenge_leads_to_shared_root_issuer(self):
        response = self.client.post(
            reverse("onec_diagnostic_mcp"),
            data=json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_1c_entities", "arguments": {}},
            }),
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            HTTP_MCP_PROTOCOL_VERSION=MCP_PROTOCOL_VERSION,
            HTTP_ORIGIN="https://chatgpt.com",
        )
        self.assertEqual(response.status_code, 200)
        challenge = response.json()["result"]["_meta"]["mcp/www_authenticate"][0]
        self.assertIn(
            f'resource_metadata="{ROOT}/.well-known/oauth-protected-resource/mcp/1c"',
            challenge,
        )
        protected = self.client.get(
            reverse("onec_diagnostic_mcp_protected_resource_metadata")
        ).json()
        self.assertEqual(protected["authorization_servers"], [ROOT])
