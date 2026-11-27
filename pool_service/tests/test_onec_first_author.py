import base64
import hashlib
import json
from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_mcp_auth import CHATGPT_CLIENT_ID_METADATA_URL
from pool_service.models import (
    FinanceMcpAccessToken, FinanceMcpClient, FinanceMcpGrant, FinanceMcpPrincipal,
    FinanceMcpPrincipalOrganization, FinanceMcpRefreshToken, Organization, OrganizationAccess,
)
from pool_service.onec_draft_models import OneCDraftRequest, OneCWriteFence
from pool_service.operations_mcp_auth import ONEC_DRAFTS, OPERATIONS, _organization_scope
from pool_service.services.onec_write_connection import FreshWriter, ITEMS
from pool_service.services.onec_write_documents import AUTHOR, EMPTY_GUID, ORDER
from pool_service.tests.test_onec_writes import BASE, FreshEndpoint, SCHEMA
from pool_service.tests.test_operations_mcp import REDIRECT

SITE = "https://service2.example"


class DraftFresh(FreshEndpoint):
    auto_author = None

    def seed(self, entity, **data):
        if self.auto_author and AUTHOR in data:
            data = {**data, AUTHOR: self.auto_author}
        return super().seed(entity, **data)


@override_settings(ONEC_MCP_DIRECT_DRAFTS_ENABLED=True, SITE_URL=SITE, ADVISOR_FINANCE_MCP_AUTH_ISSUER=SITE,
                   ONEC_ODATA_BASE_URL=BASE, ONEC_ODATA_USERNAME="reader", ONEC_ODATA_PASSWORD="test-read",
                   ONEC_MCP_DRAFT_USERNAME="draft-service", ONEC_MCP_DRAFT_PASSWORD="test-write")
class FirstAuthorTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Пример")
        self.actor = get_user_model().objects.create_user("test-manager")
        OrganizationAccess.objects.create(user=self.actor, organization=self.org, role="manager")
        principal = FinanceMcpPrincipal.objects.create(subject="test-first-author", display_name="Test")
        FinanceMcpPrincipalOrganization.objects.create(principal=principal, organization=self.org, granted_by=self.actor)
        self.app = FinanceMcpClient.objects.create(client_id=CHATGPT_CLIENT_ID_METADATA_URL, display_name="Test",
            principal=principal, client_type="public", redirect_uris=[REDIRECT], client_metadata_sha256="c"*64,
            client_metadata_verified_at=timezone.now())
        self.fresh = DraftFresh()
        self.fresh.seed(ITEMS, IsFolder=False, Артикул="001", Code="1", ЕдиницаИзмерения_Key=self.fresh.unit)
        self.enterContext(override_settings(ONEC_ODATA_ORGANIZATION_GUIDS=[self.fresh.organ],
            ONEC_ODATA_TARGET_ORGANIZATION_ID=self.org.pk, ADVISOR_OPERATIONS_MCP_ORGANIZATION_ID=self.org.pk,
            ONEC_MCP_WRITE_BINDINGS={str(self.actor.pk): "person"}))
        self.enterContext(patch.dict("os.environ", {f"ONEC_MCP_WRITE_PASSWORD_{self.actor.pk}": "test-person"}))
        self.enterContext(patch.dict(SCHEMA[ORDER], {AUTHOR: "Edm.Guid"}))
        self.enterContext(patch("pool_service.services.onec_write_connection.build_opener", return_value=self.fresh))
        self.doc = {"document_type": "ЗаказПоставщику", "organization": "Организация", "counterparty": "Поставщик",
                    "lines": [{"item": "Товар", "article": "001", "quantity": "1"}]}

    def token(self, policy=ONEC_DRAFTS, actor=None):
        scope, resource = policy.scope, SITE+"/"+policy.path
        grant = FinanceMcpGrant.objects.create(client=self.app, principal=self.app.principal, authorized_by=actor or self.actor,
            scopes=[scope, _organization_scope(self.org.pk, policy)], resource=resource)
        raw = uuid4().hex
        FinanceMcpAccessToken.objects.create(token_hash=hashlib.sha256(raw.encode()).hexdigest(), grant=grant,
            audience=resource, scopes=[scope], expires_at=timezone.now()+timedelta(minutes=10))
        return raw

    def rpc(self, method, params, token=None):
        headers = {"HTTP_AUTHORIZATION": "Bearer "+token} if token else {}
        return self.client.post(reverse("onec_drafts_mcp"), data=json.dumps({"jsonrpc": "2.0", "id": 1,
            "method": method, "params": params}), content_type="application/json", HTTP_ACCEPT="application/json", **headers)

    def call(self, name, args, token=None):
        return self.rpc("tools/call", {"name": name, "arguments": args}, token).json()["result"]

    def test_staff_oauth_code_exchange_write_audience_and_current_role(self):
        self.client.force_login(self.actor)
        verifier = "q"*48
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        resource = SITE+"/"+ONEC_DRAFTS.path
        params = {"client_id": self.app.client_id, "redirect_uri": REDIRECT, "resource": resource,
                  "response_type": "code", "state": "test-draft-state", "code_challenge_method": "S256",
                  "code_challenge": challenge, "scope": ONEC_DRAFTS.scope+" offline_access"}
        consent = self.client.get("/oauth/finance/authorize", params)
        self.assertContains(consent, "Черновики 1С")
        self.assertContains(consent, "Заполнение автора при дальнейшей записи зависит от настроек вашей 1С")
        self.assertNotContains(consent, "Автором станет первый сотрудник")
        approved = self.client.post("/oauth/finance/authorize", {**params, "decision": "approve",
            "consent_binding": consent.context["consent_binding"]})
        code = parse_qs(urlsplit(approved.url).query)["code"][0]
        from urllib.parse import urlencode
        body = {"client_id": self.app.client_id, "code": code, "code_verifier": verifier, "redirect_uri": REDIRECT,
                "grant_type": "authorization_code", "resource": resource}
        swapped = self.client.post("/oauth/finance/token", urlencode(body), content_type="application/x-www-form-urlencoded")
        self.assertEqual(swapped.status_code, 200)
        token = swapped.json()["access_token"]
        self.assertEqual(self.rpc("tools/list", {}, token).status_code, 200)
        OrganizationAccess.objects.filter(user=self.actor).delete()
        self.assertEqual(self.rpc("tools/list", {}, token).status_code, 401)

    def test_browser_old_codes_and_unrelated_tokens_cannot_create(self):
        self.client.force_login(self.actor)
        self.assertNotContains(self.client.get(reverse("onec_drafts_home")), "Получить код")
        tools = self.rpc("tools/list", {}).json()["result"]["tools"]
        self.assertTrue(all("ticket" not in t["inputSchema"]["properties"] for t in tools))
        self.assertTrue(all(t["securitySchemes"][0]["scopes"] == [ONEC_DRAFTS.scope] for t in tools))
        self.assertIn("mcp/www_authenticate", self.call("create_1c_document", self.doc)["_meta"])
        self.assertEqual(self.rpc("tools/call", {"name": "create_1c_document", "arguments": self.doc}, self.token(OPERATIONS)).status_code, 401)
        self.assertTrue(self.call("create_1c_document", {"ticket":"x"*43}, self.token())["isError"])
        self.assertFalse(OneCDraftRequest.objects.exists())
        self.assertFalse(self.fresh.posts)

    def test_initialize_reports_observed_author_without_native_guarantee(self):
        result = self.rpc("initialize", {"protocolVersion": "2025-03-26", "clientInfo": {}, "capabilities": {}}).json()["result"]
        self.assertIn("Report the observed author_empty", result["instructions"])
        self.assertIn("native 1C behavior requires separate verification", result["instructions"])
        self.assertNotIn("In 1C the first staff save or post sets the author.", result["instructions"])

    @override_settings(ADVISOR_FINANCE_MCP_RESOURCE_URL=SITE+"/mcp/finance")
    def test_refresh_sent_to_finance_cannot_revoke_draft_grant(self):
        from pool_service.finance_mcp_auth import FinanceMcpOAuthError, exchange_refresh_token
        token = self.token()
        grant = FinanceMcpAccessToken.objects.get(token_hash=hashlib.sha256(token.encode()).hexdigest()).grant
        refresh = uuid4().hex
        row = FinanceMcpRefreshToken.objects.create(grant=grant, token_hash=hashlib.sha256(refresh.encode()).hexdigest(),
            scopes=[ONEC_DRAFTS.scope], expires_at=timezone.now()+timedelta(days=1))
        with self.assertRaises(FinanceMcpOAuthError):
            exchange_refresh_token({"refresh_token": refresh, "client_id": self.app.client_id,
                                    "resource": SITE+"/mcp/finance"})
        grant.refresh_from_db()
        row.refresh_from_db()
        self.assertIsNone(grant.revoked_at)
        self.assertIsNone(row.revoked_at)
        self.assertIsNone(row.used_at)
        self.assertEqual(self.rpc("tools/list", {}, token).status_code, 200)

    def test_direct_document_technical_initial_create_and_retry_after_employee_save(self):
        token = self.token()
        first = self.call("create_1c_document", self.doc, token)
        self.assertFalse(first["isError"], first)
        self.assertEqual(first["structuredContent"]["results"][0]["status"], "created")
        body = self.fresh.posts[0][1]
        self.assertEqual((body[AUTHOR], body["Posted"]), (EMPTY_GUID, False))
        self.assertFalse(any(key.startswith("Рвк") for key in body))
        self.assertTrue(first["structuredContent"]["results"][0]["author_empty"])
        auth = "Basic "+base64.b64encode(b"draft-service:test-write").decode()
        self.assertTrue(all(a==auth for _method, _path, a in self.fresh.requests))
        row = OneCDraftRequest.objects.get()
        self.assertEqual(row.user_id, self.actor.pk)
        self.assertIsNone(row.token_hash)
        self.assertIsNone(row.approved_at)
        self.fresh.objects[ORDER][0].update({AUTHOR: str(uuid4()), "Posted": True})
        retried = self.call("create_1c_document", self.doc, token)
        self.assertFalse(retried["isError"])
        receipt = retried["structuredContent"]["results"][0]
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["verification"], "document_identity")
        self.assertFalse(receipt["author_empty"])
        self.assertEqual(len(self.fresh.posts), 1)
        self.assertEqual(OneCWriteFence.objects.count(), 1)

    def test_uncertain_request_keeps_its_original_reference_and_snapshot(self):
        token = self.token()
        self.fresh.lose_response = True
        first = self.call("create_1c_document", self.doc, token)
        self.assertTrue(first["isError"])
        original = OneCDraftRequest.objects.get().document_snapshot["body"]
        self.fresh.objects[ITEMS][0]["Артикул"] = "001-old"
        self.fresh.seed(ITEMS, IsFolder=False, Артикул="001", Code="2", ЕдиницаИзмерения_Key=self.fresh.unit)
        self.fresh.lose_response = False
        result = self.call("create_1c_document", self.doc, token)
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["results"][0]["ref_key"], original["Ref_Key"])
        self.assertEqual(len(self.fresh.posts), 1)
        with override_settings(ONEC_ODATA_ORGANIZATION_GUIDS=[str(uuid4())]):
            self.assertTrue(self.call("create_1c_document", self.doc, token)["isError"])

    def test_unknown_reassigned_uuid_blocks_retry_without_native_marker(self):
        token = self.token()
        self.fresh.assigned_key = self.fresh.lose_response = True
        self.assertTrue(self.call("create_1c_document", self.doc, token)["isError"])
        remote = self.fresh.objects[ORDER][0]
        remote.update({AUTHOR: str(uuid4()), "Posted": True, "Запасы":[]})
        self.fresh.lose_response = False
        result = self.call("create_1c_document", self.doc, token)
        self.assertTrue(result["isError"], result)
        self.assertEqual(len(self.fresh.posts), 1)
        self.assertIsNone(OneCWriteFence.objects.get().verified_at)

    def test_missing_author_field_or_write_login_never_sends(self):
        token = self.token()
        with override_settings(ONEC_MCP_DRAFT_PASSWORD=""):
            self.assertTrue(self.call("create_1c_document", self.doc, token)["isError"])
        with patch.dict(SCHEMA[ORDER], {AUTHOR:"Edm.Boolean"}):
            self.assertTrue(self.call("create_1c_document", self.doc, token)["isError"])
        for field in (AUTHOR, "Автор", "Posted"):
            self.assertTrue(self.call("create_1c_document", {**self.doc, "fields": {field:"x"}}, token)["isError"])
        self.assertFalse(self.fresh.posts)

    def test_stock_1c_filled_author_is_reported_without_claiming_first_employee(self):
        self.fresh.auto_author = str(uuid4())
        token = self.token()
        result = self.call("create_1c_document", self.doc, token)
        self.assertTrue(result["isError"], result)
        receipt = result["structuredContent"]["results"][0]
        self.assertEqual(receipt["status"], "author_not_empty")
        self.assertEqual(receipt["author_ref_key"], self.fresh.auto_author)
        self.assertFalse(receipt["author_empty"])
        self.assertEqual(receipt["number"], "D-001")
        self.assertIsNotNone(OneCWriteFence.objects.get().verified_at)
        retry = self.call("create_1c_document", self.doc, token)
        self.assertTrue(retry["isError"])
        self.assertFalse(retry["structuredContent"]["results"][0]["initial_author_empty"])
        self.assertEqual(len(self.fresh.posts), 1)

    def test_populated_author_does_not_waive_first_read_contents(self):
        token = self.token()
        self.fresh.lose_response = True
        self.assertTrue(self.call("create_1c_document", self.doc, token)["isError"])
        self.fresh.objects[ORDER][0].update({AUTHOR: str(uuid4()), "Запасы": []})
        self.fresh.lose_response = False
        self.assertTrue(self.call("create_1c_document", self.doc, token)["isError"])
        self.assertTrue(OneCWriteFence.objects.get().mismatch)
        self.assertIsNone(OneCWriteFence.objects.get().verified_at)
        self.assertEqual(len(self.fresh.posts), 1)

    def test_concurrent_verified_read_is_not_poisoned_by_later_native_changes(self):
        token = self.token()
        self.fresh.lose_response = True
        self.assertTrue(self.call("create_1c_document", self.doc, token)["isError"])
        self.fresh.lose_response = False
        remote = self.fresh.objects[ORDER][0]
        remote.update({AUTHOR: str(uuid4()), "Posted": True, "Запасы": []})
        original_active = FreshWriter.active
        def read_during_verification(writer, entity, fields, where, **kwargs):
            value = original_active(writer, entity, fields, where, **kwargs)
            if entity == ORDER and AUTHOR in fields:
                OneCWriteFence.objects.filter(verified_at__isnull=True).update(
                    verified_at=timezone.now(), author_was_empty=True)
            return value
        with patch.object(FreshWriter, "active", read_during_verification):
            result = self.call("create_1c_document", self.doc, token)
        self.assertFalse(result["isError"], result)
        receipt = result["structuredContent"]["results"][0]
        self.assertTrue(receipt["initial_author_empty"])
        self.assertFalse(receipt["author_empty"])
        self.assertEqual(receipt["verification"], "document_identity")
        self.assertFalse(OneCWriteFence.objects.get().mismatch)
        self.assertEqual(len(self.fresh.posts), 1)

    def test_successful_response_preserves_server_assigned_uuid(self):
        self.fresh.assigned_key = True
        token = self.token()
        result = self.call("create_1c_document", self.doc, token)
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["results"][0]["ref_key"], self.fresh.objects[ORDER][0]["Ref_Key"])
        self.assertFalse(self.call("create_1c_document", self.doc, token)["isError"])
        self.assertEqual(len(self.fresh.posts), 1)

    def test_other_initiator_receives_actual_author_for_existing_document(self):
        self.assertFalse(self.call("create_1c_document", self.doc, self.token())["isError"])
        author = str(uuid4())
        self.fresh.objects[ORDER][0][AUTHOR] = author
        peer = get_user_model().objects.create_user("peer-author-check")
        OrganizationAccess.objects.create(user=peer, organization=self.org, role="manager")
        result = self.call("create_1c_document", self.doc, self.token(actor=peer))
        self.assertFalse(result["isError"], result)
        receipt = result["structuredContent"]["results"][0]
        self.assertEqual(receipt["status"], "exists")
        self.assertEqual(receipt["author_ref_key"], author)
        self.assertFalse(receipt["author_empty"])
        self.assertEqual(len(self.fresh.posts), 1)

    def test_existing_invoice_keeps_reference_after_native_edit_or_deletion(self):
        doc = {**self.doc, "source_number": "DEMO", "source_date": "2026-01-02"}
        token = self.token()
        self.assertFalse(self.call("create_1c_document", doc, token)["isError"])
        remote = self.fresh.objects[ORDER][0]
        OneCWriteFence.objects.all().delete()
        OneCDraftRequest.objects.all().delete()
        self.fresh.posts.clear()
        first = self.call("create_1c_document", doc, token)
        self.assertFalse(first["isError"], first)
        self.assertEqual(first["structuredContent"]["results"][0]["status"], "exists")
        self.assertTrue(OneCWriteFence.objects.get().is_existing)
        remote["НомерВходящегоДокумента"] = "CORRECTED"
        retry = self.call("create_1c_document", doc, token)
        self.assertFalse(retry["isError"], retry)
        self.assertEqual(retry["structuredContent"]["results"][0]["ref_key"], remote["Ref_Key"])
        remote["DeletionMark"] = True
        self.assertTrue(self.call("create_1c_document", doc, token)["isError"])
        self.assertFalse(self.fresh.posts)

    def test_catalog_confirmation_is_owned_and_works_without_tickets(self):
        self.client.force_login(self.actor)
        token = self.token()
        result = self.call("request_nomenclature", {"name":"Тест", "unit":"шт", "article":"002"}, token)
        self.assertFalse(result["isError"], result)
        args = {"request_id": result["structuredContent"]["request_id"]}
        self.assertTrue(self.call("create_1c_nomenclature", args, token)["isError"])
        detail = reverse("onec_drafts_detail", args=[args["request_id"]])
        preview = self.client.get(detail)
        self.client.post(detail, {"digest":preview.context["digest"], "plan":preview.context["plan"]["digest"]})
        peer = get_user_model().objects.create_user("other-staff")
        OrganizationAccess.objects.create(user=peer, organization=self.org, role="manager")
        self.assertTrue(self.call("create_1c_nomenclature", args, self.token(actor=peer))["isError"])
        self.assertFalse(self.call("create_1c_nomenclature", args, token)["isError"])
        self.assertEqual(len(self.fresh.posts), 1)
