import hashlib
import json
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_mcp_auth import CHATGPT_CLIENT_ID_METADATA_URL
from pool_service.models import (
    FinanceMcpAccessToken,
    FinanceMcpAuditEvent,
    FinanceMcpClient,
    FinanceMcpGrant,
    FinanceMcpPrincipal,
    FinanceMcpPrincipalOrganization,
    Organization,
    OrganizationAccess,
    ServiceTask,
)
from pool_service.operations_mcp_auth import OPERATIONS_SCOPE
from pool_service.operations_mcp_policy import can_access_operations_mcp


RESOURCE = "https://service2.example/mcp/operations"
ISSUER = "https://service2.example"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"


@override_settings(
    SITE_URL=ISSUER,
    ADVISOR_OPERATIONS_MCP_ENABLED=True,
    ADVISOR_OPERATIONS_MCP_RESOURCE_URL=RESOURCE,
    ADVISOR_OPERATIONS_MCP_AUTH_ISSUER=ISSUER,
    ADVISOR_OPERATIONS_MCP_ALLOWED_ORIGINS={"https://chatgpt.com"},
)
class OperationsMcpTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Operations MCP org")
        self.owner = User.objects.create_user("operations-owner", password="test")
        self.manager = User.objects.create_user("operations-manager", password="test")
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.organization,
            role="owner",
        )
        OrganizationAccess.objects.create(
            user=self.manager,
            organization=self.organization,
            role="manager",
        )
        self.other_org = Organization.objects.create(name="Other org")
        self.other_user = User.objects.create_user("other-user", password="test")
        OrganizationAccess.objects.create(
            user=self.other_user,
            organization=self.other_org,
            role="manager",
        )

        self.principal = FinanceMcpPrincipal.objects.create(
            subject="chatgpt:service2-operations:test",
            display_name="Operations test principal",
        )
        FinanceMcpPrincipalOrganization.objects.create(
            principal=self.principal,
            organization=self.organization,
            granted_by=self.owner,
        )
        self.oauth_client = FinanceMcpClient.objects.create(
            client_id=CHATGPT_CLIENT_ID_METADATA_URL,
            display_name="ChatGPT operations test",
            client_type="public",
            redirect_uris=[REDIRECT],
            client_metadata_sha256="a" * 64,
            client_metadata_verified_at=timezone.now(),
            principal=self.principal,
        )

    def _settings(self):
        return override_settings(
            ADVISOR_OPERATIONS_MCP_ORGANIZATION_ID=str(self.organization.id),
        )

    def _token(self, *, resource=RESOURCE, scopes=None, authorized_by=None, raw="operations-token"):
        scopes = scopes or [OPERATIONS_SCOPE]
        grant = FinanceMcpGrant.objects.create(
            client=self.oauth_client,
            principal=self.principal,
            authorized_by=authorized_by or self.owner,
            scopes=scopes,
            resource=resource,
        )
        FinanceMcpAccessToken.objects.create(
            grant=grant,
            token_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            audience=resource,
            scopes=scopes,
            expires_at=timezone.now() + timedelta(minutes=10),
        )
        return raw

    def _post(self, payload, *, token=None):
        headers = {
            "HTTP_ACCEPT": "application/json",
            "HTTP_MCP_PROTOCOL_VERSION": "2025-06-18",
        }
        if token:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.post(
            reverse("operations_mcp"),
            data=json.dumps(payload),
            content_type="application/json",
            **headers,
        )

    def test_owner_admin_policy_denies_manager(self):
        self.assertTrue(can_access_operations_mcp(self.owner, self.organization))
        self.assertFalse(can_access_operations_mcp(self.manager, self.organization))
        self.assertFalse(can_access_operations_mcp(self.other_user, self.organization))

    def test_anonymous_discovery_lists_only_operations_tools(self):
        with self._settings():
            response = self._post(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            )
        self.assertEqual(response.status_code, 200)
        names = {item["name"] for item in response.json()["result"]["tools"]}
        self.assertEqual(
            names,
            {
                "list_control_tasks",
                "get_call_analysis",
                "create_task",
                "reschedule_task",
                "complete_task",
                "send_employee_notification",
            },
        )

    def test_finance_resource_token_is_rejected(self):
        raw = self._token(
            resource="https://service2.example/mcp/finance",
            scopes=["finance.read"],
            raw="finance-token",
        )
        with self._settings():
            response = self._post(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                token=raw,
            )
        self.assertEqual(response.status_code, 401)

    def test_create_task_is_idempotent_and_audited(self):
        raw = self._token()
        payload = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "create_task",
                "arguments": {
                    "idempotency_key": "call-777-send-quote",
                    "title": "Отправить клиенту расчёт",
                    "description": "Договорённость после звонка",
                    "responsible_user_id": self.manager.id,
                    "due_date": "2026-10-07",
                    "due_time": "11:30",
                    "priority": "normal",
                },
            },
        }
        with self._settings():
            first = self._post(payload, token=raw)
            second = self._post(payload, token=raw)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["result"]["isError"])
        self.assertFalse(second.json()["result"]["isError"])
        self.assertEqual(
            ServiceTask.objects.filter(
                organization=self.organization,
                payload_json__operations_mcp_idempotency_key="call-777-send-quote",
            ).count(),
            1,
        )
        task = ServiceTask.objects.get(
            payload_json__operations_mcp_idempotency_key="call-777-send-quote"
        )
        self.assertEqual(task.primary_responsible, self.manager)
        self.assertEqual(task.created_by, self.owner)
        self.assertEqual(task.end_date.isoformat(), "2026-10-07")
        self.assertTrue(
            FinanceMcpAuditEvent.objects.filter(
                grant__resource=RESOURCE,
                tool_name="operations.create_task",
                result="success",
            ).exists()
        )

    def test_cross_organization_responsible_is_rejected(self):
        raw = self._token(raw="cross-org-token")
        payload = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "create_task",
                "arguments": {
                    "idempotency_key": "cross-org",
                    "title": "Недопустимая задача",
                    "responsible_user_id": self.other_user.id,
                    "due_date": "2026-10-07",
                },
            },
        }
        with self._settings():
            response = self._post(payload, token=raw)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["result"]["isError"])
        self.assertFalse(
            ServiceTask.objects.filter(
                payload_json__operations_mcp_idempotency_key="cross-org"
            ).exists()
        )

    def test_notification_tool_is_deduplicated(self):
        raw = self._token(raw="notification-token")
        payload = {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "send_employee_notification",
                "arguments": {
                    "employee_user_id": self.manager.id,
                    "title": "Проверьте задачу",
                    "message": "Срок наступил.",
                    "dedupe_key": "task-42-due",
                },
            },
        }
        with self._settings():
            first = self._post(payload, token=raw)
            second = self._post(payload, token=raw)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["result"]["structuredContent"]["created_notifications"], 1)
        self.assertEqual(second.json()["result"]["structuredContent"]["created_notifications"], 0)
