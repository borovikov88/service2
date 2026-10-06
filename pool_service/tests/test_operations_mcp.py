import hashlib
import json
from datetime import date, timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

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
from pool_service.operations_mcp_auth import (
    OPERATIONS_SCOPE,
    _organization_scope,
    authorization_redirect_uri,
    protected_resource_metadata,
)
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
        grant_scopes = list(scopes)
        if resource == RESOURCE and OPERATIONS_SCOPE in scopes:
            grant_scopes.append(_organization_scope(self.organization.id))
        grant = FinanceMcpGrant.objects.create(
            client=self.oauth_client,
            principal=self.principal,
            authorized_by=authorized_by or self.owner,
            scopes=grant_scopes,
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

    def test_operations_uses_shared_finance_issuer(self):
        with override_settings(
            ADVISOR_FINANCE_MCP_AUTH_ISSUER="https://shared-issuer.example",
            ADVISOR_OPERATIONS_MCP_AUTH_ISSUER="https://wrong-operations-issuer.example",
        ):
            metadata = protected_resource_metadata()
            redirect_uri = authorization_redirect_uri(REDIRECT, state="issuer-test")

        self.assertEqual(
            metadata["authorization_servers"],
            ["https://shared-issuer.example"],
        )
        self.assertEqual(
            parse_qs(urlsplit(redirect_uri).query)["iss"],
            ["https://shared-issuer.example"],
        )

    def test_consent_rejects_target_change_after_page_is_shown(self):
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.other_org,
            role="owner",
        )
        FinanceMcpPrincipalOrganization.objects.create(
            principal=self.principal,
            organization=self.other_org,
            granted_by=self.owner,
        )
        self.client.force_login(self.owner)
        oauth_params = {
            "response_type": "code",
            "client_id": CHATGPT_CLIENT_ID_METADATA_URL,
            "redirect_uri": REDIRECT,
            "state": "consent-org-switch",
            "code_challenge_method": "S256",
            "code_challenge": "A" * 43,
            "resource": RESOURCE,
            "scope": OPERATIONS_SCOPE,
        }

        with self._settings():
            shown = self.client.get(reverse("operations_mcp_authorize"), oauth_params)
        self.assertEqual(shown.status_code, 200)
        consent_binding = shown.context["consent_binding"]

        post_data = dict(oauth_params)
        post_data.update({
            "decision": "approve",
            "consent_binding": consent_binding,
        })
        with override_settings(
            ADVISOR_OPERATIONS_MCP_ORGANIZATION_ID=str(self.other_org.id),
        ):
            approved = self.client.post(
                reverse("operations_mcp_authorize"),
                data=post_data,
            )

        self.assertEqual(approved.status_code, 302)
        query = parse_qs(urlsplit(approved["Location"]).query)
        self.assertEqual(query.get("error"), ["access_denied"])
        self.assertFalse(FinanceMcpGrant.objects.exists())

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

    def test_diagnostic_resource_token_is_rejected(self):
        raw = self._token(
            resource="https://service2.example/mcp/1c",
            scopes=["onec.diagnostic.read"],
            raw="diagnostic-token",
        )
        with self._settings():
            response = self._post(
                {"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {}},
                token=raw,
            )
        self.assertEqual(response.status_code, 401)

    def test_token_stops_working_after_owner_role_is_removed(self):
        raw = self._token(raw="revoked-by-role-token")
        access = OrganizationAccess.objects.get(
            user=self.owner,
            organization=self.organization,
        )
        access.role = "manager"
        access.save(update_fields=["role"])
        with self._settings():
            response = self._post(
                {"jsonrpc": "2.0", "id": 22, "method": "tools/list", "params": {}},
                token=raw,
            )
        self.assertEqual(response.status_code, 401)

    def test_token_is_bound_to_consented_organization(self):
        raw = self._token(raw="organization-bound-token")
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.other_org,
            role="owner",
        )
        FinanceMcpPrincipalOrganization.objects.create(
            principal=self.principal,
            organization=self.other_org,
            granted_by=self.owner,
        )
        with override_settings(
            ADVISOR_OPERATIONS_MCP_ORGANIZATION_ID=str(self.other_org.id),
        ):
            response = self._post(
                {"jsonrpc": "2.0", "id": 23, "method": "tools/list", "params": {}},
                token=raw,
            )
        self.assertEqual(response.status_code, 401)

    def test_audit_failure_rolls_back_task_write(self):
        raw = self._token(raw="audit-rollback-token")
        payload = {
            "jsonrpc": "2.0",
            "id": 24,
            "method": "tools/call",
            "params": {
                "name": "create_task",
                "arguments": {
                    "idempotency_key": "audit-rollback",
                    "title": "Не должна сохраниться",
                    "responsible_user_id": self.manager.id,
                    "due_date": "2026-10-07",
                },
            },
        }

        from pool_service import operations_mcp_views

        real_audit = operations_mcp_views._audit

        def fail_success_audit(authenticated, name, *, result, started, response_bytes=0):
            if result == "success":
                raise RuntimeError("audit unavailable")
            return real_audit(
                authenticated,
                name,
                result=result,
                started=started,
                response_bytes=response_bytes,
            )

        with self._settings(), patch(
            "pool_service.operations_mcp_views._audit",
            side_effect=fail_success_audit,
        ):
            response = self._post(payload, token=raw)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["result"]["isError"])
        self.assertFalse(
            ServiceTask.objects.filter(
                organization=self.organization,
                payload_json__operations_mcp_idempotency_key="audit-rollback",
            ).exists()
        )

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

    def test_reschedule_task_is_idempotent(self):
        raw = self._token(raw="reschedule-token")
        task = ServiceTask.objects.create(
            organization=self.organization,
            title="Перезвонить клиенту",
            start_date=date(2026, 10, 7),
            end_date=date(2026, 10, 7),
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            source_type=ServiceTask.SOURCE_MANAGER,
            status=ServiceTask.STATUS_NEW,
            visibility=ServiceTask.VISIBILITY_PRIVATE,
            primary_responsible=self.manager,
            created_by=self.owner,
        )
        task.responsibles.add(self.manager)
        payload = {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "reschedule_task",
                "arguments": {
                    "task_id": task.id,
                    "due_date": "2026-10-08",
                    "reason": "Клиент попросил позвонить завтра",
                },
            },
        }
        with self._settings():
            first = self._post(payload, token=raw)
            second = self._post(payload, token=raw)
        self.assertTrue(first.json()["result"]["structuredContent"]["changed"])
        self.assertFalse(second.json()["result"]["structuredContent"]["changed"])
        self.assertEqual(task.changes.filter(action="moved").count(), 1)

    def test_notification_tool_is_deduplicated(self):
        raw = self._token(raw="notification-token")
        task = ServiceTask.objects.create(
            organization=self.organization,
            title="Проверить обещание клиента",
            start_date=date(2026, 10, 7),
            end_date=date(2026, 10, 7),
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            source_type=ServiceTask.SOURCE_MANAGER,
            status=ServiceTask.STATUS_NEW,
            visibility=ServiceTask.VISIBILITY_PRIVATE,
            primary_responsible=self.manager,
            created_by=self.owner,
        )
        task.responsibles.add(self.manager)
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
                    "task_id": task.id,
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
        task.refresh_from_db()
        self.assertEqual(
            task.payload_json["operations_notification_keys"],
            [f"{self.manager.id}:task-42-due"],
        )

    def test_non_crm_task_cannot_be_completed(self):
        raw = self._token(raw="non-crm-token")
        task = ServiceTask.objects.create(
            organization=self.organization,
            title="Плановый сервисный выезд",
            start_date=date(2026, 10, 7),
            end_date=date(2026, 10, 7),
            task_type=ServiceTask.TYPE_SCHEDULED_VISIT,
            source_type=ServiceTask.SOURCE_SYSTEM,
            status=ServiceTask.STATUS_NEW,
            visibility=ServiceTask.VISIBILITY_PRIVATE,
            primary_responsible=self.manager,
            created_by=self.owner,
        )
        task.responsibles.add(self.manager)
        payload = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "complete_task",
                "arguments": {"task_id": task.id},
            },
        }
        with self._settings():
            response = self._post(payload, token=raw)
        self.assertTrue(response.json()["result"]["isError"])
        task.refresh_from_db()
        self.assertIsNone(task.completed_at)
        self.assertEqual(task.status, ServiceTask.STATUS_NEW)

    def test_notification_rejects_cancelled_task(self):
        raw = self._token(raw="cancelled-notification-token")
        task = ServiceTask.objects.create(
            organization=self.organization,
            title="Отменённая задача",
            start_date=date(2026, 10, 7),
            end_date=date(2026, 10, 7),
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            source_type=ServiceTask.SOURCE_MANAGER,
            status=ServiceTask.STATUS_CANCELLED,
            visibility=ServiceTask.VISIBILITY_PRIVATE,
            primary_responsible=self.manager,
            created_by=self.owner,
        )
        task.responsibles.add(self.manager)
        payload = {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {
                "name": "send_employee_notification",
                "arguments": {
                    "employee_user_id": self.manager.id,
                    "title": "Не отправлять",
                    "message": "Отменённая задача не должна уведомлять.",
                    "dedupe_key": "cancelled-task",
                    "task_id": task.id,
                },
            },
        }
        with self._settings():
            response = self._post(payload, token=raw)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["result"]["isError"])

    def test_notification_cannot_target_unrelated_employee(self):
        raw = self._token(raw="participant-token")
        unrelated = User.objects.create_user("unrelated-manager", password="test")
        OrganizationAccess.objects.create(
            user=unrelated,
            organization=self.organization,
            role="manager",
        )
        task = ServiceTask.objects.create(
            organization=self.organization,
            title="Отправить КП",
            start_date=date(2026, 10, 7),
            end_date=date(2026, 10, 7),
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            source_type=ServiceTask.SOURCE_MANAGER,
            status=ServiceTask.STATUS_NEW,
            visibility=ServiceTask.VISIBILITY_PRIVATE,
            primary_responsible=self.manager,
            created_by=self.owner,
        )
        task.responsibles.add(self.manager)
        payload = {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "tools/call",
            "params": {
                "name": "send_employee_notification",
                "arguments": {
                    "employee_user_id": unrelated.id,
                    "title": "Сообщение",
                    "message": "Не должно быть отправлено",
                    "dedupe_key": "unrelated",
                    "task_id": task.id,
                },
            },
        }
        with self._settings():
            response = self._post(payload, token=raw)
        self.assertTrue(response.json()["result"]["isError"])
