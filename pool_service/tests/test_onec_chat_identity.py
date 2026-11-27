"""Conversation isolation and browser identity are independent of the OAuth owner."""
import hashlib
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree as ET

from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Employee, FinanceMcpAccessToken, Organization, OrganizationAccess
from pool_service.onec_draft_models import OneCChatIdentity, OneCDraftRequest, OneCWriteFence
from pool_service.onec_chat_views import return_url
from pool_service.reward_models import OneCAuthorIdentity
from pool_service.services import onec_chat_identity as identity
from pool_service.services.onec_draft_requests import DraftUnavailable
from pool_service.services.onec_write_connection import ITEMS
from pool_service.services.onec_write_documents import AUTHOR, ORDER
from pool_service.tests import test_onec_first_author as first_author
from pool_service.tests.test_onec_writes import BASE, CAT, SCHEMA, xml_schema

USERS = "Catalog_Пользователи"
SITE = first_author.SITE
EDM = "{http://schemas.microsoft.com/ado/2009/11/edm}"


def author_schema():
    root = ET.fromstring(xml_schema())
    node = root.find(f"{EDM}EntityType[@Name='{ORDER}']")
    ET.SubElement(node, EDM + "NavigationProperty", Name="Автор", Relationship="StandardODATA.AuthorLink",
                  FromRole="Document", ToRole="User")
    link = ET.SubElement(root, EDM + "Association", Name="AuthorLink")
    ET.SubElement(link, EDM + "End", Role="Document", Type="StandardODATA." + ORDER, Multiplicity="*")
    ET.SubElement(link, EDM + "End", Role="User", Type="StandardODATA." + USERS, Multiplicity="0..1")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


@override_settings(ONEC_MCP_DIRECT_DRAFTS_ENABLED=True, ONEC_MCP_CHAT_IDENTITY_ENABLED=True,
    SITE_URL=SITE, ADVISOR_FINANCE_MCP_AUTH_ISSUER=SITE,
    ONEC_ODATA_BASE_URL=BASE, ONEC_ODATA_USERNAME="reader", ONEC_ODATA_PASSWORD="test-read",
    ONEC_MCP_DRAFT_USERNAME="draft-service", ONEC_MCP_DRAFT_PASSWORD="test-write")
class ChatIdentityTests(TestCase):
    token = first_author.FirstAuthorTests.token

    def setUp(self):
        self.enterContext(patch.dict(SCHEMA, {USERS: {**CAT, "Недействителен": "Edm.Boolean", "Служебный": "Edm.Boolean"}}))
        first_author.FirstAuthorTests.setUp(self)
        self.enterContext(patch("pool_service.tests.test_onec_writes.xml_schema", side_effect=author_schema))
        self.peer = get_user_model().objects.create_user("synthetic-peer", first_name="Другой сотрудник")
        OrganizationAccess.objects.create(user=self.peer, organization=self.org, role="manager")
        self.author, self.mapping = self.map_user(self.actor, "Сотрудник А")
        self.peer_author, self.peer_mapping = self.map_user(self.peer, "Сотрудник Б")
        self.bearer = self.token()

    def map_user(self, user, name):
        ref = self.fresh.seed(USERS, Description=name, Недействителен=False, Служебный=False)
        employee = Employee.objects.create(organization=self.org, user=user, display_name=name)
        mapping = OneCAuthorIdentity.objects.create(organization=self.org, employee=employee,
            onec_user_id=ref, raw_name=name, status="mapped", confirmed_by=self.actor, confirmed_at=timezone.now())
        return ref, mapping

    def rpc(self, method, params, token=None):
        return self.client.post(reverse("onec_drafts_mcp"), data=json.dumps({"jsonrpc": "2.0", "id": 1,
            "method": method, "params": params}), content_type="application/json", HTTP_ACCEPT="application/json",
            HTTP_AUTHORIZATION="Bearer " + (token or self.bearer))

    def call(self, name="get_chat_identity", args=None, *, chat="chat-a", subject="person-a", token=None, metadata=True):
        params = {"name": name, "arguments": args or {}}
        if metadata:
            params["_meta"] = {"openai/session": chat, "openai/subject": subject}
        return self.rpc("tools/call", params, token).json()["result"]

    def start(self, **kwargs):
        result = self.call(**kwargs)
        self.assertEqual(result["structuredContent"]["status"], "identity_required", result)
        return urlsplit(result["_meta"]["confirmation_url"]).path

    def confirm(self, user=None, **kwargs):
        path = self.start(**kwargs)
        self.client.force_login(user or self.actor)
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        response = self.client.post(path, {"account_binding": page.context["account_binding"]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["done"], response.context["message"])
        return path

    def test_no_metadata_or_model_argument_can_confirm_or_write(self):
        for metadata in (None, {}, {"openai/session": "chat"}, {"openai/session": " ", "openai/subject": "x"}):
            result = self.rpc("tools/call", {"name": "create_1c_document", "arguments": self.doc, "_meta": metadata}).json()["result"]
            self.assertTrue(result["isError"])
        result = self.call("create_1c_document", {**self.doc, "openai/session": "chat-a", "openai/subject": "person-a"}, metadata=False)
        self.assertTrue(result["isError"])
        self.assertFalse(OneCChatIdentity.objects.exists())
        self.assertFalse(OneCDraftRequest.objects.exists())
        self.assertFalse(self.fresh.posts)

    def test_confirmation_get_and_csrf_protected_post_never_create_documents(self):
        path = self.start(name="create_1c_document", args=self.doc)
        self.client.force_login(self.actor)
        page = self.client.get(path)
        self.assertContains(page, "Сотрудник А")
        self.assertNotContains(page, "Поставщик")
        self.assertIsNone(OneCChatIdentity.objects.get().confirmed_at)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.actor)
        self.assertEqual(csrf.post(path, {"account_binding": page.context["account_binding"]}).status_code, 403)
        self.client.post(path, {"account_binding": page.context["account_binding"]})
        self.assertIsNotNone(OneCChatIdentity.objects.get().confirmed_at)
        self.assertFalse(OneCDraftRequest.objects.exists())
        self.assertFalse(self.fresh.posts)

    def test_parallel_chats_keep_different_authors_without_changing_oauth_owner(self):
        self.confirm()
        self.confirm(self.peer, chat="chat-b")
        for chat, author, user in (("chat-a", self.author, self.actor), ("chat-b", self.peer_author, self.peer)):
            result = self.call("create_1c_document", self.doc, chat=chat)
            self.assertFalse(result["isError"], result)
            self.assertEqual(self.fresh.posts[-1][1][AUTHOR], author)
            self.assertIs(self.fresh.posts[-1][1]["Posted"], False)
            row = OneCDraftRequest.objects.get(chat_identity__user=user)
            self.assertEqual((row.user_id, str(row.confirmed_author)), (user.pk, author))
            self.assertTrue(result["structuredContent"]["results"][0]["initial_author_matches"])
        grant = FinanceMcpAccessToken.objects.get(token_hash=hashlib.sha256(self.bearer.encode()).hexdigest()).grant
        self.assertEqual(grant.authorized_by_id, self.actor.pk)
        self.assertEqual(self.call()["structuredContent"]["employee_name"], "Сотрудник А")

    def test_changed_chat_subject_or_grant_requires_new_identity(self):
        self.confirm()
        for kwargs in ({"chat": "other"}, {"subject": "other"}, {"token": self.token()}):
            self.assertEqual(self.call(**kwargs)["structuredContent"]["status"], "identity_required")
        self.assertEqual(OneCChatIdentity.objects.filter(confirmed_at__isnull=False).count(), 1)

    def test_confirmation_cannot_switch_accounts_or_reuse_old_account_binding(self):
        path = self.start()
        self.client.force_login(self.actor)
        page = self.client.get(path)
        original = page.context["account_binding"]
        self.client.force_login(self.peer)
        response = self.client.post(path, {"account_binding": original})
        self.assertFalse(response.context["done"])
        self.assertIsNone(OneCChatIdentity.objects.get().confirmed_at)
        page = self.client.get(path)
        self.client.post(path, {"account_binding": page.context["account_binding"]})
        self.client.force_login(self.actor)
        self.assertEqual(self.client.post(path, {"account_binding": original}).status_code, 404)
        self.assertEqual(OneCChatIdentity.objects.get().user_id, self.peer.pk)

    def test_expired_challenge_rotates_and_old_link_never_confirms(self):
        old = self.start()
        OneCChatIdentity.objects.update(challenge_expires_at=timezone.now()-timedelta(seconds=1))
        new = self.start()
        self.assertNotEqual(old, new)
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(old).status_code, 404)
        self.assertIsNone(OneCChatIdentity.objects.get().confirmed_at)

    def test_mapping_change_or_ambiguity_closes_existing_chat(self):
        self.confirm()
        self.mapping.onec_user_id = self.peer_author
        # Remove the competing mapping before changing this employee's mapping.
        self.peer_mapping.delete()
        self.mapping.save(update_fields=["onec_user_id"])
        self.assertTrue(self.call("create_1c_document", self.doc)["isError"])
        self.mapping.onec_user_id = self.author
        self.mapping.save(update_fields=["onec_user_id"])
        OneCAuthorIdentity.objects.create(organization=self.org, employee=self.mapping.employee,
            onec_user_id=str(uuid4()), status="mapped", confirmed_by=self.actor, confirmed_at=timezone.now())
        self.assertTrue(self.call()["isError"])
        self.assertFalse(self.fresh.posts)

    def test_missing_mapping_and_inactive_native_author_fail_closed(self):
        path = self.start()
        self.mapping.confirmed_at = None
        self.mapping.save(update_fields=["confirmed_at"])
        self.client.force_login(self.actor)
        page = self.client.get(path)
        self.client.post(path, {"account_binding": page.context["account_binding"]})
        self.assertIsNone(OneCChatIdentity.objects.get().confirmed_at)
        self.mapping.confirmed_at = timezone.now()
        self.mapping.save(update_fields=["confirmed_at"])
        self.confirm()
        self.fresh.objects[USERS][0]["Недействителен"] = True
        self.assertTrue(self.call("create_1c_document", self.doc)["isError"])
        self.assertFalse(self.fresh.posts)

    def test_revoked_employee_or_chat_denies_existing_receipt(self):
        self.confirm(self.peer)
        self.assertFalse(self.call("create_1c_document", self.doc)["isError"])
        Employee.objects.filter(pk=self.peer_mapping.employee_id).update(employment_status="dismissed")
        self.assertTrue(self.call("create_1c_document", self.doc)["isError"])
        Employee.objects.filter(pk=self.peer_mapping.employee_id).update(employment_status="employed")
        OneCChatIdentity.objects.update(revoked_at=timezone.now())
        self.assertTrue(self.call("create_1c_document", self.doc)["isError"])
        self.assertEqual(len(self.fresh.posts), 1)

    def test_revoked_oauth_prevents_browser_confirmation_and_tool_access(self):
        path = self.start()
        grant = OneCChatIdentity.objects.get().grant
        grant.revoked_at = timezone.now()
        grant.save(update_fields=["revoked_at"])
        self.client.force_login(self.actor)
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.rpc("tools/list", {}).status_code, 401)

    def test_lost_post_reconciles_once_and_preserves_initial_author_check(self):
        self.confirm()
        self.fresh.lose_response = True
        self.assertTrue(self.call("create_1c_document", self.doc)["isError"])
        self.fresh.lose_response = False
        result = self.call("create_1c_document", self.doc)
        self.assertFalse(result["isError"], result)
        self.assertTrue(result["structuredContent"]["results"][0]["initial_author_matches"])
        self.fresh.objects[ORDER][0].update({AUTHOR: self.peer_author, "Posted": True})
        result = self.call("create_1c_document", self.doc)
        self.assertFalse(result["isError"], result)
        self.assertTrue(result["structuredContent"]["results"][0]["initial_author_matches"])
        self.assertEqual(len(self.fresh.posts), 1)

    def test_unexpected_native_author_is_sticky_and_never_recreated(self):
        self.confirm()
        self.fresh.auto_author = self.peer_author
        first = self.call("create_1c_document", self.doc)
        self.assertTrue(first["isError"], first)
        receipt = first["structuredContent"]["results"][0]
        self.assertEqual((receipt["status"], receipt["number"]), ("author_mismatch", "D-001"))
        self.assertFalse(OneCWriteFence.objects.get().author_matches)
        self.fresh.objects[ORDER][0][AUTHOR] = self.author
        self.assertTrue(self.call("create_1c_document", self.doc)["isError"])
        self.assertEqual(len(self.fresh.posts), 1)

    def test_cards_preview_and_digest_are_bound_to_same_chat(self):
        self.confirm()
        preview = self.call("request_nomenclature", {"name": "Тест", "unit": "шт", "article": "TEST-2"})
        self.assertFalse(preview["isError"], preview)
        content = preview["structuredContent"]
        self.assertNotIn("review_url", content)
        self.assertFalse(self.fresh.posts)
        args = {key: content[key] for key in ("request_id", "confirmation_digest")}
        self.assertTrue(self.call("create_1c_nomenclature", {**args, "confirmation_digest": "wrong"})["isError"])
        self.confirm(chat="chat-b")
        self.assertTrue(self.call("create_1c_nomenclature", args, chat="chat-b")["isError"])
        result = self.call("create_1c_nomenclature", args)
        self.assertFalse(result["isError"], result)
        self.assertEqual(self.fresh.posts[0][0], ITEMS)
        self.assertFalse(self.call("create_1c_nomenclature", args)["isError"])
        self.assertEqual(len(self.fresh.posts), 1)
        detail = self.client.get(reverse("onec_drafts_detail", args=[content["request_id"]]))
        self.assertTrue(detail.context["history_only"])

    def test_cards_mapping_revoked_after_preview_blocks_creation(self):
        self.confirm()
        content = self.call("request_nomenclature", {"name": "Тест", "unit": "шт", "article": "TEST-3"})["structuredContent"]
        self.mapping.status = "excluded"
        self.mapping.save(update_fields=["status"])
        args = {key: content[key] for key in ("request_id", "confirmation_digest")}
        self.assertTrue(self.call("create_1c_nomenclature", args)["isError"])
        self.assertFalse(self.fresh.posts)

    def test_chatgpt_return_survives_login_and_only_allows_exact_https_host(self):
        path = self.start()
        callback = "https://chatgpt.com/c/synthetic-chat?resume=identity"
        response = self.client.get(path, {"redirectUrl": callback})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("redirectUrl", response.url)
        self.client.force_login(self.actor)
        page = self.client.get(path)
        response = self.client.post(path, {"account_binding": page.context["account_binding"]})
        self.assertEqual(response.url, callback)
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        for url in ("https://chatgpt.com.evil.example/c/x", "http://chatgpt.com/c/x", "https://other@chatgpt.com/c/x", "https://chatgpt.com:444/c/x", "//chatgpt.com/c/x"):
            self.assertEqual(return_url(url), "")

    def test_widget_contract_and_new_mode_instructions(self):
        tools = self.rpc("tools/list", {}).json()["result"]["tools"]
        tool = next(t for t in tools if t["name"] == "get_chat_identity")
        self.assertTrue(tool["_meta"]["openai/widgetAccessible"])
        uri = tool["_meta"]["openai/outputTemplate"]
        content = self.rpc("resources/read", {"uri": uri}).json()["result"]["contents"][0]
        self.assertIn("openExternal", content["text"])
        self.assertIn("redirect_domains", content["_meta"]["openai/widgetCSP"])
        self.assertNotIn("ticket", json.dumps(tools))
        self.client.force_login(self.actor)
        self.assertContains(self.client.get(reverse("onec_drafts_home")), "подтвердите личность")

    def test_widget_external_return_and_expired_link_refresh(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node is required for the host bridge contract")
        result = subprocess.run([node, str(Path(__file__).with_name("onec_chat_identity.widget.cjs"))],
            capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


@override_settings(ONEC_MCP_DIRECT_DRAFTS_ENABLED=True, ONEC_MCP_CHAT_IDENTITY_ENABLED=True,
    SITE_URL=SITE, ADVISOR_FINANCE_MCP_AUTH_ISSUER=SITE,
    ONEC_ODATA_BASE_URL=BASE, ONEC_ODATA_USERNAME="reader", ONEC_ODATA_PASSWORD="test-read",
    ONEC_MCP_DRAFT_USERNAME="draft-service", ONEC_MCP_DRAFT_PASSWORD="test-write")
class ChatIdentityLockTests(TransactionTestCase):
    setUp = ChatIdentityTests.setUp
    map_user = ChatIdentityTests.map_user
    token = ChatIdentityTests.token
    rpc = ChatIdentityTests.rpc
    call = ChatIdentityTests.call
    start = ChatIdentityTests.start

    @skipUnlessDBFeature("has_select_for_update_nowait")
    def test_cross_grant_confirmation_locks_org_first_and_observes_revocation(self):
        self.start(token=self.token(actor=self.peer))
        row = OneCChatIdentity.objects.get()
        token = identity.confirmation_token(row)
        waiting, failures = Event(), []

        def observe(execute, sql, params, many, context):
            if "FOR UPDATE" in sql and "pool_service_organization" in sql:
                waiting.set()
            return execute(sql, params, many, context)

        def confirm_in_connection():
            close_old_connections()
            try:
                with connection.execute_wrapper(observe):
                    identity.confirm(token, self.actor)
            except Exception as exc:
                failures.append(exc)
            finally:
                connection.close()

        worker = Thread(target=confirm_in_connection, daemon=True)
        try:
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=self.org.pk)
                owner_grant = FinanceMcpAccessToken.objects.get(
                    token_hash=hashlib.sha256(self.bearer.encode()).hexdigest()).grant
                type(row.grant).objects.select_for_update().select_related(
                    "client__principal", "principal", "authorized_by").get(pk=owner_grant.pk)
                worker.start()
                self.assertTrue(waiting.wait(10), "Confirmation never reached its lock")
                # The other grant/user cannot be held before waiting on this org.
                grant = type(row.grant).objects.select_for_update(nowait=True).select_related(
                    "client__principal", "principal", "authorized_by").get(pk=row.grant_id)
                grant.revoked_at = timezone.now()
                grant.save(update_fields=["revoked_at"])
        finally:
            worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(failures), 1, failures)
        self.assertIsInstance(failures[0], DraftUnavailable)
        row.refresh_from_db()
        self.assertIsNone(row.confirmed_at)
