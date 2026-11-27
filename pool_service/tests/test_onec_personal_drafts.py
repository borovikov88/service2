import hashlib
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.onec_draft_models import OneCDraftRequest
from pool_service.services import onec_draft_requests as drafts
from pool_service.services.onec_draft_intent import InvalidIntent, document_intent, nomenclature_intent


class PersonalOneCDraftTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Example")
        self.actor = get_user_model().objects.create_user("employee", password="example-login")
        self.peer = get_user_model().objects.create_user("other")
        for user in (self.actor, self.peer):
            OrganizationAccess.objects.create(organization=self.org, user=user, role="manager")
        override = override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.org.pk))
        override.enable()
        self.addCleanup(override.disable)
        self.payload = {"document_type": "Заказ поставщику", "counterparty": "Поставщик",
                        "lines": [{"item": "Средство 20 л", "quantity": "10,00", "article": "00071"}]}
        self.client.force_login(self.actor)

    def rpc(self, method, params, **headers):
        return self.client.post(reverse("onec_drafts_mcp"),
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}),
            content_type="application/json", HTTP_ACCEPT="application/json, text/event-stream", **headers)

    def test_personal_code_is_ephemeral_and_absent_from_history(self):
        response = self.client.post(reverse("onec_drafts_home"), {"kind": "document"})
        self.assertEqual(response.status_code, 200)
        ticket = response.context["ticket"]
        row = OneCDraftRequest.objects.get()
        self.assertEqual(row.token_hash, hashlib.sha256(ticket.encode()).hexdigest())
        self.assertNotContains(self.client.get(reverse("onec_drafts_home")), ticket)
        OneCDraftRequest.objects.filter(pk=row.pk).update(expires_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaises(drafts.DraftUnavailable):
            drafts.submit(ticket, "document", self.payload)

    def test_repeat_is_idempotent_and_cannot_replace_or_approve(self):
        row, ticket = drafts.issue(self.actor.pk, "document")
        self.assertEqual(drafts.submit(ticket, "document", self.payload), row.pk)
        self.assertEqual(drafts.submit(ticket, "document", self.payload), row.pk)
        with self.assertRaises(drafts.DraftUnavailable):
            drafts.submit(ticket, "document", {**self.payload, "counterparty": "Другой"})
        with self.assertRaises(drafts.DraftUnavailable):
            drafts.submit(ticket, "nomenclature", {"name": "Новинка", "unit": "шт"})
        row.refresh_from_db()
        self.assertEqual(row.intent["lines"][0]["quantity"], "10")
        self.assertEqual(OneCDraftRequest.objects.count(), 1)

    def test_revoked_or_inactive_actor_cannot_submit_or_read(self):
        row, ticket = drafts.issue(self.actor.pk, "document")
        OrganizationAccess.objects.filter(user=self.actor).delete()
        with self.assertRaises(drafts.DraftUnavailable):
            drafts.submit(ticket, "document", self.payload)
        self.assertEqual(self.client.get(reverse("onec_drafts_detail", args=[row.pk])).status_code, 404)
        OrganizationAccess.objects.create(user=self.actor, organization=self.org, role="manager")
        Employee.objects.create(user=self.actor, organization=self.org, display_name="Пример", is_active=False)
        with self.assertRaises(drafts.DraftUnavailable):
            drafts.submit(ticket, "document", self.payload)

    def test_accountant_with_only_finance_role_uses_personal_review_in_target_org(self):
        OrganizationAccess.objects.filter(user=self.actor).update(role="accountant")
        home = reverse("onec_drafts_home")
        self.assertEqual(self.client.get(home).status_code, 200)
        response = self.client.post(home, {"kind": "document"})
        self.assertEqual(response.status_code, 200)
        row = response.context["issued"]
        drafts.submit(response.context["ticket"], "document", self.payload)
        self.assertEqual(self.client.get(reverse("onec_drafts_detail", args=[row.pk])).status_code, 200)
        other = Organization.objects.create(name="Other example")
        OrganizationAccess.objects.filter(user=self.actor).update(organization=other)
        self.assertEqual(self.client.get(home).status_code, 404)
        self.assertEqual(self.client.post(home, {"kind": "document"}).status_code, 404)

    def test_requests_are_organization_and_owner_scoped(self):
        row, ticket = drafts.issue(self.actor.pk, "document")
        self.client.force_login(self.peer)
        self.assertEqual(self.client.get(reverse("onec_drafts_detail", args=[row.pk])).status_code, 404)
        with override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.org.pk + 100)):
            with self.assertRaises(drafts.DraftUnavailable):
                drafts.submit(ticket, "document", self.payload)
            self.assertEqual(self.client.get(reverse("onec_drafts_home")).status_code, 404)

    @override_settings(ONEC_DRAFTS_HOURLY_LIMIT=2)
    def test_rolling_limit_covers_both_kinds(self):
        drafts.issue(self.actor.pk, "document")
        drafts.issue(self.actor.pk, "nomenclature")
        with self.assertRaises(drafts.DraftLimit):
            drafts.issue(self.actor.pk, "document")
        drafts.issue(self.peer.pk, "document")

    def test_browser_csrf_required_for_personal_ticket_and_mcp_session_cannot_write(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.actor)
        self.assertEqual(strict.post(reverse("onec_drafts_home"), {"kind": "document"}).status_code, 403)
        self.assertTrue(self.rpc("tools/call", {"name": "prepare_document", "arguments": self.payload}).json()["result"]["isError"])
        self.assertFalse(OneCDraftRequest.objects.exists())
        self.assertEqual(self.rpc("tools/list", {}, HTTP_ORIGIN="https://outside.example").status_code, 403)

    def test_actual_transport_does_not_expose_other_data_or_posting(self):
        self.client.logout()
        tools = self.rpc("tools/list", {}).json()["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"prepare_document", "request_nomenclature", "create_1c_document", "create_1c_nomenclature"})
        row, ticket = drafts.issue(self.actor.pk, "document")
        result = self.rpc("tools/call", {"name": "prepare_document", "arguments": {**self.payload, "ticket": ticket}}).json()["result"]
        self.assertFalse(result["isError"])
        self.assertIn(str(row.pk), result["content"][0]["text"])
        self.assertNotIn(self.payload["counterparty"], result["content"][0]["text"])
        self.assertEqual(self.client.get(reverse("onec_drafts_detail", args=[row.pk])).status_code, 302)

    def test_login_returns_to_private_review_only_for_local_resolved_route(self):
        row, _ = drafts.issue(self.actor.pk, "document")
        detail = reverse("onec_drafts_detail", args=[row.pk])
        self.client.logout()
        login = self.client.get(detail)
        self.assertEqual(login.status_code, 302)
        response = self.client.post(login.url, {"username": "employee", "password": "example-login"})
        self.assertRedirects(response, detail)
        self.client.logout()
        external = reverse("login") + "?next=https://outside.example/onec/drafts/"
        response = self.client.post(external, {"username": "employee", "password": "example-login"})
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("outside.example", response.url)

    def test_invalid_xml_characters_do_not_consume_ticket(self):
        row, ticket = drafts.issue(self.actor.pk, "document")
        for codepoint in (0xD800, 0xFFFE, 0xFFFF):
            bad = {"item": "x" + chr(codepoint), "quantity": "2"}
            with self.assertRaises(InvalidIntent):
                drafts.submit(ticket, "document", {**self.payload, "lines": [bad]})
        self.assertEqual(drafts.submit(ticket, "document", self.payload), row.pk)

    def test_separate_explicit_nomenclature_request(self):
        row, ticket = drafts.issue(self.actor.pk, "nomenclature")
        intent = {"name": "Товар", "unit": "канистра", "packaging": "20 л"}
        drafts.submit(ticket, "nomenclature", intent)
        response = self.client.get(reverse("onec_drafts_detail", args=[row.pk]))
        self.assertContains(response, "20 л")

    def test_rejects_mutations_and_malformed_quantities(self):
        for key in ("posted", "Posted", "actor", "confirm", "url", "Ref_Key"):
            with self.subTest(key=key), self.assertRaises(InvalidIntent):
                document_intent({**self.payload, key: True})
        for qty in (True, 1, "0", "-1", "1e2", "1.0000007"):
            with self.subTest(qty=qty), self.assertRaises(InvalidIntent):
                document_intent({**self.payload, "lines": [{"item": "a", "quantity": qty}]})
        with self.assertRaises(InvalidIntent):
            document_intent({**self.payload, "lines": self.payload["lines"] * 101})
        with self.assertRaises(InvalidIntent):
            nomenclature_intent({"name": "Товар", "unit": "шт", "auto_create": True})
