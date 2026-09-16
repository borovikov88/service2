import base64
import hashlib
import json
from datetime import timedelta
from unittest.mock import patch
import uuid

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.odata_profit import ODataConfig
from pool_service.finance_mcp_auth import CHATGPT_CLIENT_ID_METADATA_URL, FINANCE_READ_SCOPE
from pool_service.models import (
    FinanceMcpAccessToken,
    FinanceMcpClient,
    FinanceMcpGrant,
    FinanceMcpPrincipal,
    FinanceMcpPrincipalOrganization,
    Organization,
    OrganizationAccess,
)
from pool_service.onec_diagnostic_mcp_auth import (
    DIAGNOSTIC_READ_SCOPE,
    authenticate_bearer_header,
    authorization_is_allowed,
    exchange_token,
    issue_authorization_code,
    validate_authorization_request,
)
from pool_service.onec_diagnostic_mcp_views import MCP_PROTOCOL_VERSION, TOOL_NAMES


DIAGNOSTIC_RESOURCE = "https://service2.example.test/mcp/1c"
FINANCE_RESOURCE = "https://service2.example.test/mcp/finance"
REDIRECT_URI = "https://chatgpt.com/connector_platform_oauth_redirect"
ORG_GUID = "10000000-0000-4000-8000-000000000001"

MCP_SETTINGS = {
    "ADVISOR_ONEC_DIAGNOSTIC_MCP_ENABLED": True,
    "ADVISOR_FINANCE_MCP_ENABLED": True,
    "SITE_URL": "https://service2.example.test",
    "ADVISOR_ONEC_DIAGNOSTIC_MCP_RESOURCE_URL": DIAGNOSTIC_RESOURCE,
    "ADVISOR_ONEC_DIAGNOSTIC_MCP_AUTH_ISSUER": "https://service2.example.test/onec-diagnostic",
    "ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_ORIGINS": {"https://chatgpt.com"},
    "ADVISOR_FINANCE_MCP_RESOURCE_URL": FINANCE_RESOURCE,
    "ADVISOR_FINANCE_MCP_AUTH_ISSUER": "https://service2.example.test",
    "ADVISOR_FINANCE_MCP_ALLOWED_ORIGINS": {"https://chatgpt.com"},
    "ONEC_ODATA_BASE_URL": "https://1c.example.test/odata/standard.odata/",
    "ONEC_ODATA_USERNAME": "reader",
    "ONEC_ODATA_PASSWORD": "secret",
    "ONEC_ODATA_ORGANIZATION_GUIDS": (ORG_GUID,),
    "ONEC_ODATA_TIMEOUT_SECONDS": 5,
    "ONEC_ODATA_MAX_PAGES": 5,
    "ONEC_ODATA_MAX_ROWS": 50,
}


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _pkce(verifier):
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")


@override_settings(**MCP_SETTINGS)
class OneCDiagnosticMcpTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("diag-mcp-owner", password="pass")
        self.accountant = User.objects.create_user("diag-mcp-accountant", password="pass")
        self.manager = User.objects.create_user("diag-mcp-manager", password="pass")
        self.organization = Organization.objects.create(name="Diagnostic MCP target")
        for user, role in (
            (self.owner, "owner"),
            (self.accountant, "accountant"),
            (self.manager, "manager"),
        ):
            OrganizationAccess.objects.create(user=user, organization=self.organization, role=role)
        self.target_override = override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=self.organization.pk
        )
        self.target_override.enable()
        self.addCleanup(self.target_override.disable)

        self.principal = FinanceMcpPrincipal.objects.create(
            subject="chatgpt:onec-diagnostic-test:v1",
            display_name="1C Diagnostic Test",
        )
        FinanceMcpPrincipalOrganization.objects.create(
            principal=self.principal,
            organization=self.organization,
            granted_by=self.owner,
        )
        self.oauth_client = FinanceMcpClient.objects.create(
            client_id=CHATGPT_CLIENT_ID_METADATA_URL,
            display_name="ChatGPT Diagnostic Test",
            principal=self.principal,
            redirect_uris=[REDIRECT_URI],
            client_metadata_sha256="a" * 64,
            client_metadata_verified_at=timezone.now(),
        )
        self.raw_diagnostic_token = self._create_access_token(
            resource=DIAGNOSTIC_RESOURCE,
            scope=DIAGNOSTIC_READ_SCOPE,
        )
        self.raw_finance_token = self._create_access_token(
            resource=FINANCE_RESOURCE,
            scope=FINANCE_READ_SCOPE,
        )

    def _create_access_token(self, *, resource, scope, authorized_by=None):
        grant = FinanceMcpGrant.objects.create(
            client=self.oauth_client,
            principal=self.principal,
            authorized_by=authorized_by or self.owner,
            scopes=[scope],
            resource=resource,
        )
        raw = f"{scope}-" + uuid.uuid4().hex
        FinanceMcpAccessToken.objects.create(
            grant=grant,
            token_hash=_hash(raw),
            audience=resource,
            scopes=[scope],
            expires_at=timezone.now() + timedelta(minutes=5),
        )
        return raw

    def _headers(self, token=None):
        return {
            "HTTP_AUTHORIZATION": f"Bearer {token or self.raw_diagnostic_token}",
            "HTTP_ACCEPT": "application/json",
            "HTTP_MCP_PROTOCOL_VERSION": MCP_PROTOCOL_VERSION,
            "HTTP_ORIGIN": "https://chatgpt.com",
            "content_type": "application/json",
        }

    def _post_mcp(self, payload, token=None, **extra):
        headers = self._headers(token)
        headers.update(extra)
        content_type = headers.pop("content_type")
        return self.client.post(
            reverse("onec_diagnostic_mcp"),
            data=json.dumps(payload),
            content_type=content_type,
            **headers,
        )

    def test_bearer_challenge_precedes_body_parsing(self):
        response = self.client.post(
            reverse("onec_diagnostic_mcp"),
            data=b"not-json",
            content_type="application/json",
            HTTP_ACCEPT="application/json",
            HTTP_ORIGIN="https://chatgpt.com",
        )
        self.assertEqual(response.status_code, 401)
        self.assertIn("onec.diagnostic.read", response["WWW-Authenticate"])

    def test_cross_resource_tokens_are_isolated(self):
        response = self._post_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            token=self.raw_finance_token,
        )
        self.assertEqual(response.status_code, 401)

        response = self.client.post(
            reverse("finance_mcp"),
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw_diagnostic_token}",
            HTTP_ACCEPT="application/json",
            HTTP_MCP_PROTOCOL_VERSION=MCP_PROTOCOL_VERSION,
            HTTP_ORIGIN="https://chatgpt.com",
        )
        self.assertEqual(response.status_code, 401)

    def test_tools_list_exposes_exact_read_only_surface(self):
        response = self._post_mcp(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        self.assertEqual(response.status_code, 200)
        tools = response.json()["result"]["tools"]
        self.assertEqual(tuple(item["name"] for item in tools), TOOL_NAMES)
        for tool in tools:
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertFalse(tool["inputSchema"]["additionalProperties"])

    def test_raw_query_and_organization_overrides_are_rejected(self):
        forbidden = ("url", "$filter", "$select", "credentials", "nextLink", "organization_guid")
        for field in forbidden:
            with self.subTest(field=field):
                with patch("pool_service.onec_diagnostic_mcp_views.onec_diagnostic.read_entity_rows") as reader:
                    response = self._post_mcp({
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {
                            "name": "read_1c_rows",
                            "arguments": {
                                "entity_set": "Document_Test",
                                "fields": ["Number"],
                                "filters": [{"field": "Number", "op": "eq", "value": "1"}],
                                field: "forbidden",
                            },
                        },
                    })
                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.json()["result"]["isError"])
                    reader.assert_not_called()

    def test_sales_tool_has_only_fixed_inputs_and_delegates(self):
        result = {"kind": "onec_nomenclature_sales", "complete": True, "matches": []}
        with patch(
            "pool_service.onec_diagnostic_mcp_views.onec_diagnostic.get_nomenclature_sales",
            return_value=result,
        ) as sales:
            response = self._post_mcp({
                "jsonrpc": "2.0", "id": 30, "method": "tools/call",
                "params": {"name": "get_1c_nomenclature_sales", "arguments": {
                    "query": "sand", "start_date": "2025-08-01", "end_date": "2025-08-31",
                }},
            })
        self.assertFalse(response.json()["result"]["isError"])
        sales.assert_called_once()
        for forbidden in ("entity_set", "$filter", "$select", "url", "nextLink", "organization_guid"):
            with self.subTest(forbidden=forbidden), patch(
                "pool_service.onec_diagnostic_mcp_views.onec_diagnostic.get_nomenclature_sales"
            ) as denied:
                response = self._post_mcp({
                    "jsonrpc": "2.0", "id": 31, "method": "tools/call",
                    "params": {"name": "get_1c_nomenclature_sales", "arguments": {
                        "query": "sand", "start_date": "2025-08-01", "end_date": "2025-08-31",
                        forbidden: "unsafe",
                    }},
                })
                self.assertTrue(response.json()["result"]["isError"])
                denied.assert_not_called()

    def test_read_tool_delegates_to_foundation_gateway(self):
        config = ODataConfig(
            base_url=MCP_SETTINGS["ONEC_ODATA_BASE_URL"],
            username="reader",
            password="secret",
            organization_guids=(ORG_GUID,),
            timeout_seconds=5,
            max_pages=5,
            max_rows=50,
        )
        result = {"kind": "onec_diagnostic_rows", "entity_set": "Document_Test", "row_count": 1, "rows": [{"Number": "1"}]}
        with patch("pool_service.onec_diagnostic_mcp_views.onec_diagnostic.config_from_settings", return_value=config), patch(
            "pool_service.onec_diagnostic_mcp_views.onec_diagnostic.read_entity_rows",
            return_value=result,
        ) as reader:
            response = self._post_mcp({
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "read_1c_rows",
                    "arguments": {
                        "entity_set": "Document_Test",
                        "fields": ["Number"],
                        "filters": [{"field": "Number", "op": "eq", "value": "secret-filter-value"}],
                        "top": 5,
                    },
                },
            })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["result"]["isError"])
        reader.assert_called_once_with(
            config,
            "Document_Test",
            fields=["Number"],
            filters=[{"field": "Number", "op": "eq", "value": "secret-filter-value"}],
            top=5,
        )

    def test_audit_stores_metadata_not_filter_values_or_rows(self):
        config = ODataConfig(
            base_url=MCP_SETTINGS["ONEC_ODATA_BASE_URL"],
            username="reader",
            password="secret",
            organization_guids=(ORG_GUID,),
            max_pages=5,
            max_rows=50,
        )
        with patch("pool_service.onec_diagnostic_mcp_views.onec_diagnostic.config_from_settings", return_value=config), patch(
            "pool_service.onec_diagnostic_mcp_views.onec_diagnostic.read_entity_rows",
            return_value={"kind": "onec_diagnostic_rows", "rows": [{"Number": "private-row-value"}]},
        ):
            self._post_mcp({
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "read_1c_rows",
                    "arguments": {
                        "entity_set": "Document_Test",
                        "fields": ["Number"],
                        "filters": [{"field": "Number", "op": "eq", "value": "private-filter-value"}],
                    },
                },
            })
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT tool_name, entity_set, selected_fields, result FROM pool_service_onecdiagnosticmcpauditevent ORDER BY id DESC LIMIT 1"
            )
            row = cursor.fetchone()
        self.assertEqual(row[0], "read_1c_rows")
        self.assertEqual(row[1], "Document_Test")
        self.assertEqual(json.loads(row[2]), ["Number"])
        serialized = " ".join(str(item) for item in row)
        self.assertNotIn("private-filter-value", serialized)
        self.assertNotIn("private-row-value", serialized)
        self.assertNotIn(self.raw_diagnostic_token, serialized)

    def test_owner_and_accountant_can_authorize_but_manager_cannot(self):
        self.assertTrue(authorization_is_allowed(self.owner, self.oauth_client))
        self.assertTrue(authorization_is_allowed(self.accountant, self.oauth_client))
        self.assertFalse(authorization_is_allowed(self.manager, self.oauth_client))

    def test_authorization_code_flow_is_resource_bound_and_does_not_revoke_finance(self):
        verifier = "v" * 64
        finance_grant = FinanceMcpGrant.objects.filter(
            resource=FINANCE_RESOURCE, revoked_at__isnull=True
        ).first()
        authorization = validate_authorization_request({
            "response_type": "code",
            "client_id": CHATGPT_CLIENT_ID_METADATA_URL,
            "redirect_uri": REDIRECT_URI,
            "scope": f"{DIAGNOSTIC_READ_SCOPE} offline_access",
            "state": "state-1",
            "code_challenge": _pkce(verifier),
            "code_challenge_method": "S256",
            "resource": DIAGNOSTIC_RESOURCE,
        })
        code = issue_authorization_code(authorization=authorization, user=self.owner)
        finance_grant.refresh_from_db()
        self.assertIsNone(finance_grant.revoked_at)
        token_response = exchange_token({
            "grant_type": "authorization_code",
            "code": code,
            "client_id": CHATGPT_CLIENT_ID_METADATA_URL,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
            "resource": DIAGNOSTIC_RESOURCE,
        })
        self.assertIn("access_token", token_response)
        self.assertIn("refresh_token", token_response)
        authenticated = authenticate_bearer_header(
            "Bearer " + token_response["access_token"]
        )
        self.assertEqual(authenticated.grant.resource, DIAGNOSTIC_RESOURCE)

    def test_inactive_authorizer_invalidates_existing_diagnostic_token(self):
        token = self._create_access_token(
            resource=DIAGNOSTIC_RESOURCE,
            scope=DIAGNOSTIC_READ_SCOPE,
            authorized_by=self.accountant,
        )
        self.accountant.is_active = False
        self.accountant.save(update_fields=["is_active"])
        response = self._post_mcp(
            {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}},
            token=token,
        )
        self.assertEqual(response.status_code, 401)

    def test_metadata_isolated_from_finance_authorization_server(self):
        protected = self.client.get(reverse("onec_diagnostic_mcp_protected_resource_metadata"))
        self.assertEqual(protected.status_code, 200)
        self.assertEqual(protected.json()["resource"], DIAGNOSTIC_RESOURCE)
        self.assertEqual(protected.json()["scopes_supported"], [DIAGNOSTIC_READ_SCOPE])
        self.assertEqual(
            protected.json()["authorization_servers"],
            [MCP_SETTINGS["ADVISOR_FINANCE_MCP_AUTH_ISSUER"]],
        )

        auth_server = self.client.get(reverse("onec_diagnostic_mcp_authorization_server_metadata"))
        self.assertEqual(auth_server.status_code, 200)
        data = auth_server.json()
        self.assertEqual(data["issuer"], MCP_SETTINGS["ADVISOR_FINANCE_MCP_AUTH_ISSUER"])
        self.assertEqual(
            data["authorization_endpoint"],
            f'{MCP_SETTINGS["ADVISOR_FINANCE_MCP_AUTH_ISSUER"]}/oauth/finance/authorize',
        )
        self.assertEqual(
            data["token_endpoint"],
            f'{MCP_SETTINGS["ADVISOR_FINANCE_MCP_AUTH_ISSUER"]}/oauth/finance/token',
        )
        self.assertIn(DIAGNOSTIC_READ_SCOPE, data["scopes_supported"])

    def test_origin_and_transport_are_fail_closed(self):
        response = self.client.options(
            reverse("onec_diagnostic_mcp"),
            HTTP_ORIGIN="https://evil.example",
        )
        self.assertEqual(response.status_code, 403)

        response = self.client.get(reverse("onec_diagnostic_mcp"), HTTP_ORIGIN="https://chatgpt.com")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response["Allow"], "POST, OPTIONS")
