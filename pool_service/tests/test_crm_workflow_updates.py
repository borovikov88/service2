from datetime import timedelta
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from pool_service.client_crm_models import ClientBusinessRole, ClientBusinessRoleEvent, ClientImportCandidate
from pool_service.communication_models import CallAnalysis, CommunicationAccess, Conversation, ConversationMessage
from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import CrmAccess, CrmCommunicationRoute, CrmDeal, CrmLead, CrmLeadDealLink, CrmLeadSource, CrmPipeline
from pool_service.crm_order_models import CrmDealDocumentLink, CrmOneCDocument
from pool_service.models import Client, Employee, OrganizationAccess
from pool_service.services.contact_roles import reconcile_onec_roles
from pool_service.services.crm_commercial_commands import CommercialConflict
from pool_service.services.crm_communication_routing import assign_route
from pool_service.services.crm_document_links import document_candidates, document_projection, execute_document_command
from pool_service.services.crm_order_finance import finance_projection, mark_crm_order_sync_error
from pool_service.staff_call_models import CallPrivacyHold
from pool_service.tests.test_crm_contact_episodes import EpisodeFixture
from pool_service.tests.test_crm_order_finance import CURRENCY, EXTERNAL_ORG, ORDER, PARTY, OrderFinanceFixture


@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class DealRoutingTests(EpisodeFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.grant.view_team_deals = self.grant.edit_own_deals = True
        self.grant.save()
        bootstrap_commercial_settings(self.org.pk)
        self.pipeline = CrmPipeline.objects.get(organization=self.org, code="construction")
        self.stage = self.pipeline.stages.get(code="need")

    def deal(self, manager=None):
        return CrmDeal.objects.create(organization=self.org, client=self.person, contact=self.person, pipeline=self.pipeline,
            stage=self.stage, title="Synthetic active deal", manager=manager or self.user)

    def message(self, key):
        message = self.incoming(external_message_id=key)
        conversation = message.conversation
        conversation.client = self.person
        conversation.save(update_fields=["client"])
        return self.deliver("message")

    def test_calls_and_messages_reuse_active_deal_and_closed_deal_allows_a_new_lead(self):
        deal = self.deal()
        for direction in ("in", "out"):
            self.call(type=direction)
            self.assertEqual(self.deliver("call").error_code, "deal_linked")
        message = self.message("synthetic-message")
        self.assertEqual(message.error_code, "deal_linked")
        self.assertEqual(set(CrmCommunicationRoute.objects.values_list("deal_id", flat=True)), {deal.pk})
        self.assertFalse(CrmLead.objects.exists())
        self.client.force_login(self.user)
        rows = self.client.get(reverse("crm_deal_activity", args=[deal.pk]), {"org_id": self.org.pk}).json()["items"]
        self.assertEqual(sum(row.get("relation") == "deal_source" for row in rows), 3)
        deal.stage = self.pipeline.stages.get(code="completed")
        deal.save(update_fields=["stage"])
        self.assertEqual(self.message("synthetic-new-episode").error_code, "lead_created")
        self.assertEqual(CrmLead.objects.count(), 1)
        self.assertEqual(deal.outcome, "open")

    def test_ambiguous_route_can_be_assigned_once_and_future_dialogue_keeps_its_context(self):
        self.deal()
        chosen = self.deal()
        source = self.message("synthetic-ambiguous")
        route = CrmCommunicationRoute.objects.get(inbox=source)
        self.assertIsNone(route.deal_id)
        self.assertEqual(source.error_code, "needs_distribution")
        action = dict(user=self.user, organization=self.org, route_id=route.pk, deal_id=chosen.pk,
            revision=route.revision, remember_dialog=True)
        self.assertEqual(assign_route(**action).deal_id, chosen.pk)
        self.assertEqual(assign_route(**action).deal_id, chosen.pk)
        future = self.message("synthetic-follow-up")
        self.assertEqual(CrmCommunicationRoute.objects.get(inbox=future).deal_id, chosen.pk)
        self.assertEqual(CrmCommunicationRoute.objects.count(), 2)
        self.assertFalse(CrmLeadSource.objects.exists())

    def test_shared_phone_with_known_company_requires_a_deal_decision(self):
        company = Client.objects.create(organization=self.org, name="Synthetic company", client_type="legal")
        Client.objects.create(organization=self.org, name="Synthetic coworker", phone=self.person.phone)
        deal = self.deal()
        deal.client = company
        deal.save(update_fields=["client"])
        self.call(person=company)
        event = self.deliver("call")
        self.assertEqual(event.error_code, "needs_distribution")
        self.assertIsNone(CrmCommunicationRoute.objects.get(inbox=event).deal_id)
        self.assertFalse(CrmLead.objects.exists())

    def test_staff_reply_is_captured_once_and_appears_in_deal_activity(self):
        deal = self.deal()
        self.message("synthetic-reply-context")
        conversation = Conversation.objects.get(connection=self.connection)
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_reply_conversations=True)
        self.client.force_login(self.user)
        response = self.client.post(reverse("communication_reply", args=[conversation.uuid]) + f"?org_id={self.org.pk}",
            {"body":"Synthetic working reply"})
        self.assertEqual(response.status_code, 302)
        reply = ConversationMessage.objects.get(conversation=conversation, direction="out")
        event = self.deliver("message")
        self.assertEqual((event.external_id, event.error_code), (f"reply:{reply.pk}", "deal_linked"))
        self.assertEqual(CrmCommunicationRoute.objects.get(inbox=event).deal_id, deal.pk)
        rows = self.client.get(reverse("crm_deal_activity", args=[deal.pk]),
            {"org_id":self.org.pk, "type":"message"}).json()["items"]
        self.assertIn(("message:" + str(reply.pk), "deal_source"), {(row["id"], row["relation"]) for row in rows})

    def test_responsible_filters_preserve_closed_view_and_no_assignee(self):
        deal = self.deal()
        free = CrmLead.objects.create(organization=self.org, title="Synthetic free")
        closed = CrmLead.objects.create(organization=self.org, title="Synthetic completed", assignee=self.user,
            status="closed", result="no_deal", reason="Synthetic resolution")
        self.client.force_login(self.user)
        params = {"org_id":self.org.pk}
        leads = reverse("crm_live_lead_data")
        deals = reverse("crm_live_data")
        self.assertEqual([row["id"] for row in self.client.get(leads, {**params, "owner":"unassigned"}).json()["leads"]], [free.pk])
        self.assertEqual([row["id"] for row in self.client.get(leads, {**params, "owner":self.user.pk,
            "mode":"closed"}).json()["leads"]], [closed.pk])
        self.assertEqual([row["id"] for row in self.client.get(deals, {**params, "owner":self.user.pk}).json()["deals"]], [deal.pk])
        self.assertEqual(self.client.get(deals, {**params, "owner":"unassigned"}).json()["deals"], [])

    def test_owner_recording_and_transcript_follow_responsibility_and_live_proof(self):
        coworker = get_user_model().objects.create_user("synthetic-route-manager")
        OrganizationAccess.objects.create(user=coworker, organization=self.org, role="manager")
        Employee.objects.create(user=coworker, organization=self.org, display_name="Synthetic route manager")
        CrmAccess.objects.create(user=coworker, organization=self.org, view_team_deals=True)
        CommunicationAccess.objects.filter(user=coworker, organization=self.org).update(
            can_view_all_calls=True, can_view_own_calls=True, can_listen_calls=True)
        deal = self.deal(coworker)
        call = self.call(type="out")
        call.employee = self.user
        call.save(update_fields=["employee"])
        self.assertEqual(self.deliver("call").error_code, "deal_linked")
        proof = dict(call.intake_scope)
        self.client.force_login(coworker)
        endpoints = [reverse("communication_call_recording", args=[call.pk]),
            reverse("communication_call_analysis_transcript", args=[call.pk])]
        params = {"org_id": self.org.pk}
        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            call.recording_file.save("synthetic-route.wav", ContentFile(b"synthetic-audio"))
            CallAnalysis.objects.create(call=call, status=CallAnalysis.STATUS_READY, transcript="synthetic transcript")
            for endpoint in endpoints:
                response = self.client.get(endpoint, params)
                self.assertEqual(response.status_code, 200, endpoint)
                response.close()
            deal.manager = self.user
            deal.save(update_fields=["manager"])
            for endpoint in endpoints:
                self.assertIn(self.client.get(endpoint, params).status_code, (403, 404))
            deal.manager = coworker
            deal.save(update_fields=["manager"])
            type(call).objects.filter(pk=call.pk).update(intake_scope={**proof, "channel": 0})
            for endpoint in endpoints:
                self.assertIn(self.client.get(endpoint, params).status_code, (403, 404))
            type(call).objects.filter(pk=call.pk).update(intake_scope=proof)
            CallPrivacyHold.objects.create(call=call, organization=self.org, owner=self.user)
            for endpoint in endpoints:
                self.assertIn(self.client.get(endpoint, params).status_code, (403, 404))

    def test_restore_spam_refuses_duplicate_or_links_to_selected_active_deal(self):
        spam = CrmLead.objects.create(organization=self.org, assignee=self.user, client=self.person,
            title="Synthetic lead", status="closed", result="spam", reason="Old decision")
        action = {"column": "restore", "reason": "Synthetic verified customer"}
        active = CrmLead.objects.create(organization=self.org, assignee=self.user, client=self.person, title="Synthetic current")
        with self.assertRaises(ValidationError):
            self.command("lead.disposition", action, spam)
        self.close(active)
        deal = self.deal()
        with self.assertRaises(ValidationError):
            self.command("lead.disposition", action, spam)
        self.command("lead.disposition", {**action, "target_deal_id": deal.pk}, spam)
        spam.refresh_from_db()
        self.assertEqual((spam.status, spam.result, spam.linked_deal_id), ("closed", "linked", deal.pk))
        self.assertTrue(CrmLeadDealLink.objects.filter(lead=spam, deal=deal, kind="existing").exists())


class OperationalDocumentsTests(OrderFinanceFixture, TestCase):
    def change(self, deal, changes, **overrides):
        return execute_document_command(**{
            "actor": self.user, "organization": self.org, "operation_key": uuid4().hex,
            "subject_id": deal.pk, "expected_revision": deal.revision, "changes": changes, **overrides})

    def source_document(self, order, kind="check", order_guid=ORDER, **overrides):
        return CrmOneCDocument.objects.create(**{
            "organization":self.org, "source_id":order.source_id, "external_organization":EXTERNAL_ORG,
            "kind":kind, "guid":str(uuid4()), "customer_guid":PARTY, "order_guid":order_guid,
            "currency_guid":CURRENCY, "currency":"RUB", "number":"SYN-CHECK", "document_date":order.order_date,
            "amount":Decimal("1000.00"), "valid":True, "fetched_at":order.fetched_at, **overrides})

    def test_manager_can_link_cached_order_without_financial_authority_and_replay(self):
        deal, order = self.prepare_order_deal(realized_amount=Decimal("1000"), paid_amount=Decimal("1000"))
        mark_crm_order_sync_error(actor=self.user, organization=self.org)
        order.fetched_at -= timedelta(days=90)
        order.save(update_fields=["fetched_at"])
        self.member.role = "manager"
        self.member.save(update_fields=["role"])
        deal.revision = 0
        deal.save(update_fields=["revision"])
        self.client.force_login(self.user)
        with patch("pool_service.finance_imports.odata_crm_orders.read_crm_orders") as transport:
            page = self.client.get(reverse("crm_document_candidates"), {"org_id":self.org.pk, "deal_id":deal.pk})
            transport.assert_not_called()
        self.assertEqual(page.status_code, 200, page.content)
        self.assertEqual([row["id"] for row in page.json()["items"]], [order.pk])
        self.assertTrue(page.json()["last_failed"])
        changes = {"kind":"order", "record_id":order.pk}
        self.assertEqual(self.change(deal, changes, operation_key="test-document-replay"),
            self.change(deal, changes, operation_key="test-document-replay"))
        deal.refresh_from_db()
        self.assertEqual(deal.outcome, "open")
        self.assertEqual(CrmDealDocumentLink.objects.count(), 1)
        self.assertEqual(finance_projection(deal, self.user, self.org)["state"], "restricted")

    def test_explicit_parent_only_and_suppression_replacement_are_atomic(self):
        deal, order = self.prepare_order_deal()
        child = self.source_document(order)
        other = self.source_document(order, kind="realization", order_guid="")
        self.change(deal, {"kind":"order", "record_id":order.pk})
        deal.refresh_from_db()
        projection = document_projection(self.user, self.org, deal)
        self.assertEqual([(item["kind"], item["automatic"]) for item in projection["items"]], [("order", False), ("check", True)])
        changes = {"kind":"realization", "record_id":other.pk, "exclude_id":child.pk, "reason":"Synthetic correction"}
        with self.assertRaises(CommercialConflict):
            self.change(deal, {**changes, "record_id":other.pk + 1000})
        self.assertFalse(CrmDealDocumentLink.objects.filter(document=child).exists())
        self.change(deal, changes)
        result = document_projection(self.user, self.org, deal)
        self.assertEqual([item["kind"] for item in result["items"]], ["order", "realization"])
        self.assertTrue(CrmDealDocumentLink.objects.get(document=child).suppressed)

    def test_revoked_source_and_foreign_manager_cannot_read_cache(self):
        deal, order = self.prepare_order_deal()
        self.change(deal, {"kind":"order", "record_id":order.pk})
        member = get_user_model().objects.create_user("synthetic-unrelated-manager")
        OrganizationAccess.objects.create(organization=self.org, user=member, role="manager")
        CrmAccess.objects.create(organization=self.org, user=member, view_team_deals=True)
        with self.assertRaises(PermissionDenied):
            document_candidates(actor=member, organization=self.org, deal=deal, kind="order")
        with self.settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=0):
            data = document_projection(self.user, self.org, deal)
            self.assertTrue(data["review"])
            self.assertNotIn(order.number, str(data))

    def test_exact_check_refresh_failure_preserves_cached_result(self):
        deal, order = self.prepare_order_deal()
        request = {"deal_id":deal.pk, "kind":"check", "q":"SYN-EXACT"}
        fresh_guid = str(uuid4())
        self.client.force_login(self.user)
        lookup = reverse("crm_document_refresh") + f"?org_id={self.org.pk}"
        endpoint = "pool_service.finance_imports.odata_crm_orders.read_crm_documents"
        row = dict(order_guid=ORDER, customer_guid=PARTY, currency_guid=CURRENCY, currency="RUB", number=request["q"],
            document_date=order.order_date, amount=Decimal("1000.00"), valid=True)
        with patch(endpoint, return_value={(EXTERNAL_ORG, fresh_guid):row}) as transport:
            result = self.client.post(lookup, data=json.dumps(request), content_type="application/json")
            self.assertEqual(result.status_code, 200, result.content)
            self.assertEqual(transport.call_args.kwargs["customers"], (PARTY,))
        self.assertEqual([item["number"] for item in result.json()["items"]], [request["q"]])
        with patch(endpoint, side_effect=RuntimeError("synthetic hidden transport detail")):
            failed = self.client.post(lookup, data=json.dumps(request), content_type="application/json")
        self.assertEqual(failed.status_code, 503)
        self.assertNotIn("transport detail", failed.content.decode())
        self.assertTrue(CrmOneCDocument.objects.get(guid=fresh_guid).valid)
        self.assertEqual(document_candidates(actor=self.user, organization=self.org, deal=deal,
            kind="check", query=request["q"])["items"], result.json()["items"])

    def test_old_role_is_preserved_and_dual_role_sync_does_not_create_communications(self):
        legacy = ClientBusinessRole.objects.create(client=self.client_record, role="supplier", active=True)
        candidate = ClientImportCandidate.objects.create(organization=self.org, source_ref=PARTY,
            name="Synthetic source counterparty", payload={"business_roles":{"buyer":True, "supplier":False}})
        reconcile_onec_roles(self.client_record, candidate)
        legacy.refresh_from_db()
        self.assertTrue(legacy.active)
        self.assertFalse(legacy.onec_active)
        self.assertTrue(legacy.manual_active)
        self.assertTrue(ClientBusinessRole.objects.get(client=self.client_record, role="buyer").active)
        self.assertEqual(ClientBusinessRoleEvent.objects.filter(client=self.client_record, onec_ref=PARTY).count(), 2)
        self.assertFalse(CrmLead.objects.exists())
