import uuid
from unittest.mock import patch
from unittest import skipUnless
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import connection, close_old_connections

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, TransactionTestCase, Client as TestClient
from django.urls import reverse
from django.utils import timezone

from pool_service import crm_source_links as links
from pool_service.communication_models import (
    ChannelConnection, CommunicationAccess, CommunicationChannel, Conversation,
    ConversationMessage, PhoneCall, TelephonyConnection)
from pool_service.crm_source_models import CrmSourceLink
from pool_service.models import CrmItem, DataAuditLog, Organization, OrganizationAccess, ServiceTask


class CrmAttributionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Attribution test organization")
        self.foreign = Organization.objects.create(name="Other attribution organization")
        self.manager = User.objects.create_user("crm-source-manager", password="test")
        self.owner = User.objects.create_user("crm-source-owner", password="test")
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role="manager")
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role="owner")
        self.channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Services")
        self.connection = ChannelConnection.objects.create(pk=4, channel=self.channel,
            name="Services", external_id="446312402")
        self.goods = ChannelConnection.objects.create(pk=1, channel=self.channel, name="Goods",
            external_id="goods-test", settings={"unchanged": True})
        self.dialog = Conversation.objects.create(organization=self.org, connection=self.connection,
            external_id="source-dialog", participant_name="Source participant")
        self.item = CrmItem.objects.create(organization=self.org, direction="service",
            title="Existing request", stage="srv_new")
        self.other_item = CrmItem.objects.create(organization=self.org, direction="project",
            title="Existing project", stage="prj_idea")
        self.client.force_login(self.manager)
        self.company = patch("pool_service.crm_source_links.company_has_access", return_value=True)
        self.company.start()
        self.addCleanup(self.company.stop)
        self.http = patch("pool_service.communication_avito._json_request",
            side_effect=AssertionError("Provider requests are forbidden in source linking"))
        self.http.start()
        self.addCleanup(self.http.stop)

    def save(self, **changes):
        data = dict(item_id=self.item.pk, quality="target", reason="Manager assessment",
            expected_revision=0, operation=uuid.uuid4())
        data.update(changes)
        return links.save_link(self.manager, 4, "conversation", self.dialog.uuid, **data)

    def target(self, **params):
        base = reverse("avito_crm_attribution", args=[4])
        return base + "?conversation=" + str(self.dialog.uuid)

    def post_data(self, **values):
        data = {"source_kind": "conversation", "source_id": str(self.dialog.uuid),
            "item_id": str(self.item.pk), "next_task_id": "", "quality": "target",
            "reason": "Manager assessment", "ad_mode": "auto", "manual_ad_id": "",
            "revision": "0", "operation": str(uuid.uuid4())}
        data.update(values)
        return data

    def make_call(self):
        telephone = TelephonyConnection.objects.create(organization=self.org, name="Test telephony")
        return PhoneCall.objects.create(organization=self.org, connection=telephone,
            external_id="source-call", employee=self.manager, phone_number="+79990000001",
            direction="in", started_at=timezone.now(), source_kind="telephony")

    def test_default_manager_can_link_without_channel_management_or_new_requests(self):
        self.assertFalse(CommunicationAccess.objects.get(user=self.manager, organization=self.org).can_manage_channels)
        for index in range(3):
            ConversationMessage.objects.create(conversation=self.dialog, direction="in",
                external_id="message-" + str(index), body="PRIVATE-SOURCE-CONTENT")
        before = CrmItem.objects.count()
        link, changed = self.save()
        self.assertTrue(changed)
        self.assertEqual(CrmSourceLink.objects.count(), 1)
        self.assertEqual(link.crm_item_id, self.item.pk)
        self.assertEqual(CrmItem.objects.count(), before)
        self.assertEqual(links.coverage(self.manager, self.connection)["dialogs"], 1)
        self.assertEqual(links.coverage(self.manager, self.connection)["distinct_requests"], 1)
        self.dialog.refresh_from_db()
        self.assertEqual(self.dialog.status, "new")

    def test_same_operation_replay_dedupes_and_conflicting_or_stale_changes_reject(self):
        operation = uuid.uuid4()
        link, _ = self.save(operation=operation)
        again, changed = self.save(operation=operation)
        self.assertFalse(changed)
        self.assertEqual(again.pk, link.pk)
        self.assertEqual(DataAuditLog.objects.filter(entity_type="crm_source_link").count(), 1)
        with self.assertRaises(links.LinkConflict):
            self.save(operation=operation, reason="Different content")
        with self.assertRaises(links.LinkConflict):
            self.save(expected_revision=0)
        corrected, _ = self.save(item_id=self.other_item.pk, expected_revision=1)
        self.assertEqual(corrected.pk, link.pk)
        self.assertEqual(corrected.revision, 2)
        self.assertEqual(corrected.crm_item_id, self.other_item.pk)
        self.assertEqual(CrmSourceLink.objects.count(), 1)

    def test_replay_rechecks_source_assignment_and_revoked_capability(self):
        operation = uuid.uuid4()
        self.save(operation=operation)
        self.dialog.assignee = self.owner
        self.dialog.save(update_fields=["assignee"])
        with self.assertRaises(PermissionDenied):
            self.save(operation=operation)
        self.dialog.assignee = None
        self.dialog.save(update_fields=["assignee"])
        CommunicationAccess.objects.filter(user=self.manager, organization=self.org).update(can_take_conversation=False)
        with self.assertRaises(PermissionDenied):
            self.save(operation=operation)
        self.assertEqual(CrmSourceLink.objects.get().revision, 1)

    def test_same_org_is_not_a_global_role_union_and_foreign_sources_reject(self):
        service = User.objects.create_user("crm-source-service")
        OrganizationAccess.objects.create(organization=self.org, user=service, role="service")
        OrganizationAccess.objects.create(organization=self.foreign, user=service, role="manager")
        CommunicationAccess.objects.filter(user=service, organization=self.org).update(
            can_view_conversations=True, can_take_conversation=True)
        with self.assertRaises(PermissionDenied):
            links.save_link(service, 4, "conversation", self.dialog.uuid,
                item_id=self.other_item.pk, operation=uuid.uuid4())
        foreign_item = CrmItem.objects.create(organization=self.foreign,
            direction="service", title="FOREIGN-REQUEST", stage="srv_new")
        with self.assertRaises(PermissionDenied):
            self.save(item_id=foreign_item.pk)
        goods_dialog = Conversation.objects.create(organization=self.org, connection=self.goods,
            external_id="goods-dialog", participant_name="GOODS-PARTICIPANT")
        with self.assertRaises(PermissionDenied):
            links.save_link(self.manager, 4, "conversation", goods_dialog.uuid,
                item_id=self.item.pk, operation=uuid.uuid4())
        with self.assertRaises(PermissionDenied):
            links.scope(self.manager, 1)
        self.goods.refresh_from_db()
        self.assertEqual(self.goods.settings, {"unchanged": True})

    def test_rebinding_or_inactive_account_does_not_reuse_old_source_authority(self):
        self.connection.external_id = "other-account"
        self.connection.save(update_fields=["external_id"])
        with self.assertRaises(PermissionDenied):
            self.save()
        self.connection.external_id = "446312402"
        self.connection.is_active = False
        self.connection.save(update_fields=["external_id", "is_active"])
        with self.assertRaises(PermissionDenied):
            self.save()
        self.assertFalse(CrmSourceLink.objects.exists())

    @patch("pool_service.crm_source_links.owned_ads")
    def test_provider_and_manual_ad_evidence_are_scoped_and_do_not_overwrite_provider_json(self, owned):
        owned.return_value = {"8343962135": timezone.now()}
        self.dialog.provider_context = {"item": {"id": "8343962135"}, "preserved": "provider"}
        self.dialog.context_synced_at = timezone.now()
        self.dialog.save(update_fields=["provider_context", "context_synced_at"])
        link, _ = self.save(ad_mode="auto")
        self.assertEqual(link.ad_source, "provider")
        self.assertEqual(link.avito_item_id, "8343962135")
        self.dialog.refresh_from_db()
        self.assertEqual(self.dialog.provider_context["preserved"], "provider")
        with self.assertRaises(ValueError):
            self.save(ad_mode="manual", manual_ad_id="foreign-ad", expected_revision=1)
        self.assertEqual(CrmSourceLink.objects.get().revision, 1)

    @patch("pool_service.crm_source_links.is_private_call")
    def test_call_quality_does_not_silently_become_an_avito_inquiry_and_privacy_is_current(self, private):
        private.return_value = False
        call = self.make_call()
        link, _ = links.save_link(self.manager, 4, "call", call.pk, item_id=self.item.pk,
            quality="target", operation=uuid.uuid4(), call_origin="unknown")
        counts = links.coverage(self.manager, self.connection)
        self.assertEqual(counts["calls"], 0)
        self.assertEqual(counts["candidate_calls_unknown"], 1)
        self.assertEqual(counts["candidate_calls_linked"], 1)
        link, _ = links.save_link(self.manager, 4, "call", call.pk, item_id=self.item.pk,
            quality="target", operation=uuid.uuid4(), expected_revision=1, call_origin="manual")
        self.assertEqual(links.coverage(self.manager, self.connection)["calls"], 1)
        private.return_value = True
        with self.assertRaises(PermissionDenied):
            links.source_for(self.manager, self.connection, "call", call.pk)
        self.assertEqual(links.coverage(self.manager, self.connection)["calls"], 0)
        self.assertEqual(links.coverage(self.manager, self.connection)["candidate_calls_unknown"], 0)

    @patch("pool_service.crm_source_links.owned_ads")
    def test_manual_call_ad_requires_explicit_channel_acknowledgment(self, owned):
        owned.return_value = {"8346503274": timezone.now()}
        call = self.make_call()
        with self.assertRaises(ValueError):
            links.save_link(self.manager, 4, "call", call.pk, item_id=self.item.pk,
                operation=uuid.uuid4(), ad_mode="manual", manual_ad_id="8346503274",
                call_origin="unknown")
        self.assertFalse(CrmSourceLink.objects.exists())
        link, _ = links.save_link(self.manager, 4, "call", call.pk, item_id=self.item.pk,
            operation=uuid.uuid4(), ad_mode="manual", manual_ad_id="8346503274",
            call_origin="manual")
        self.assertEqual(link.evidence["call_origin_ack"]["actor_id"], self.manager.pk)
        self.assertEqual(link.ad_source, "manual")

    def test_unrated_visible_dialogs_are_unknown_not_zero_or_missing_and_cards_dedupe(self):
        second = Conversation.objects.create(organization=self.org, connection=self.connection,
            external_id="second-dialog", participant_name="Second")
        counts = links.coverage(self.manager, self.connection)
        self.assertEqual(counts["dialogs"], 2)
        self.assertEqual(counts["unknown_requests"], 2)
        self.save()
        links.save_link(self.manager, 4, "conversation", second.uuid, item_id=self.item.pk, operation=uuid.uuid4())
        counts = links.coverage(self.manager, self.connection)
        self.assertEqual(counts["linked_dialogs"], 2)
        self.assertEqual(counts["distinct_requests"], 1)
        self.assertIsNone(counts["payment_links"])
        self.assertIsNone(counts["revenue"])

    def test_next_step_reuses_existing_task_and_rejects_unrelated_or_private_source(self):
        task = ServiceTask.objects.create(organization=self.org, crm_item=self.item,
            title="Existing follow-up", task_type="crm_followup",
            start_date=timezone.localdate(), created_by=self.manager)
        link, _ = self.save(task_id=task.pk)
        self.assertEqual(link.next_task_id, task.pk)
        self.assertEqual(ServiceTask.objects.count(), 1)
        other = ServiceTask.objects.create(organization=self.org, crm_item=self.other_item,
            title="Wrong target", start_date=timezone.localdate(), created_by=self.manager)
        with self.assertRaises(PermissionDenied):
            self.save(task_id=other.pk, expected_revision=1)
        with patch("pool_service.crm_source_links.task_source_is_private", return_value=True):
            with self.assertRaises(PermissionDenied):
                self.save(task_id=task.pk, expected_revision=1)

    def test_audit_stores_provenance_not_message_or_reason_copies(self):
        self.save(reason="PRIVATE-MANAGER-REASON")
        event = DataAuditLog.objects.get(entity_type="crm_source_link")
        serialized = str(event.before) + str(event.after)
        self.assertNotIn("PRIVATE-MANAGER-REASON", serialized)
        self.assertNotIn("PRIVATE-SOURCE-CONTENT", serialized)
        self.assertIn("reason_sha256", event.after)

    def test_manager_cannot_change_or_indirectly_retarget_a_protected_order(self):
        link, _ = self.save()
        link.order_guid = uuid.uuid4()
        link.save(update_fields=["order_guid"])
        with self.assertRaises(PermissionDenied):
            self.save(expected_revision=1, order_action="clear")
        with self.assertRaises(PermissionDenied):
            self.save(expected_revision=1, item_id=self.other_item.pk)
        response = self.client.get(self.target())
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, str(link.order_guid))
        self.assertNotContains(response, "name=\"order_guid\"")
        self.assertIsNone(response.context["coverage"]["observed_order_references"])

    def test_manager_workflow_lookup_navigation_and_post_are_available_without_admin_capability(self):
        response = self.client.get(self.target())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Existing request")
        self.assertNotContains(response, "name=\"order_guid\"")
        self.assertIn("no-store", response.headers["Cache-Control"])
        lookup = self.client.get(reverse("avito_crm_request_lookup", args=[4]),
            {"conversation": str(self.dialog.uuid), "q": "Existing"})
        self.assertEqual(lookup.status_code, 200)
        self.assertIn(str(self.item.pk), {row["id"] for row in lookup.json()["results"]})
        before = CrmItem.objects.count()
        posted = self.client.post(self.target(), self.post_data())
        self.assertEqual(posted.status_code, 302)
        self.assertEqual(CrmItem.objects.count(), before)
        self.assertEqual(CrmSourceLink.objects.count(), 1)
        comm = self.client.get(reverse("communications_conversations"),
            {"conversation": self.dialog.uuid})
        self.assertContains(comm, "Оценка обращения и связь с CRM")

    def test_form_csrf_and_forged_finance_post_do_not_write(self):
        strict = TestClient(enforce_csrf_checks=True)
        strict.force_login(self.manager)
        self.assertEqual(strict.post(self.target(), self.post_data()).status_code, 403)
        forged = self.client.post(self.target(), self.post_data(
            order_action="set", order_guid=str(uuid.uuid4())))
        self.assertEqual(forged.status_code, 403)
        self.assertFalse(CrmSourceLink.objects.exists())

    def test_foreign_request_is_not_revealed_in_search(self):
        CrmItem.objects.create(organization=self.foreign, direction="service",
            title="SECRET-FOREIGN-TITLE", stage="srv_new")
        response = self.client.get(reverse("avito_crm_request_lookup", args=[4]),
            {"conversation": str(self.dialog.uuid), "q": "SECRET"})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "SECRET-FOREIGN-TITLE")
        self.client.logout()
        self.assertEqual(self.client.get(self.target()).status_code, 302)

    def test_model_requires_exactly_one_source(self):
        invalid = CrmSourceLink(organization=self.org, avito_connection=self.connection)
        with self.assertRaises(ValidationError):
            invalid.full_clean()

    @patch("pool_service.crm_source_orders.observed")
    def test_financial_reference_replay_revalidates_current_source_and_audit_has_only_hash(self, observed):
        guid = uuid.uuid4()
        observed.return_value = {"guid": guid, "evidence": {
            "source": "active_money_forecast_planning_snapshot", "business_sha256": "test-business"}}
        operation = uuid.uuid4()
        link, _ = links.save_link(self.owner, 4, "conversation", self.dialog.uuid,
            item_id=self.item.pk, order_action="set", order_guid=str(guid), operation=operation)
        self.assertEqual(link.order_guid, guid)
        self.assertTrue(observed.call_args.kwargs["lock"])
        event = DataAuditLog.objects.get(entity_type="crm_source_link")
        self.assertNotIn(str(guid), str(event.before) + str(event.after))
        self.assertIn("order_guid_sha256", event.after)
        observed.return_value = None
        with self.assertRaises(ValueError):
            links.save_link(self.owner, 4, "conversation", self.dialog.uuid,
                item_id=self.item.pk, order_action="set", order_guid=str(guid), operation=operation)
        self.assertEqual(DataAuditLog.objects.filter(entity_type="crm_source_link").count(), 1)

    def test_lookup_does_not_expose_same_org_card_with_foreign_client_graph(self):
        from pool_service.models import Client
        foreign_client = Client.objects.create(organization=self.foreign, name="Foreign client")
        malformed = CrmItem.objects.create(organization=self.org, direction="service",
            title="PRIVATE-MALFORMED-REQUEST", client=foreign_client, stage="srv_new")
        lookup = self.client.get(reverse("avito_crm_request_lookup", args=[4]),
            {"conversation": str(self.dialog.uuid), "q": "PRIVATE-MALFORMED"})
        self.assertEqual(lookup.status_code, 200)
        self.assertNotContains(lookup, "PRIVATE-MALFORMED-REQUEST")
        page = self.client.get(self.target())
        self.assertNotContains(page, "PRIVATE-MALFORMED-REQUEST")
        with self.assertRaises(PermissionDenied):
            self.save(item_id=malformed.pk)

    def test_sidebar_and_get_reject_call_with_foreign_telephony_or_foreign_source_binding(self):
        call = self.make_call()
        original_connection = call.connection
        foreign_connection = TelephonyConnection.objects.create(organization=self.foreign, name="Foreign telephony")
        PhoneCall.objects.filter(pk=call.pk).update(connection=foreign_connection)
        page = self.client.get(reverse("avito_crm_attribution", args=[4]))
        self.assertNotIn(call.pk, {row["id"] for row in page.context["calls"]})
        denied = self.client.get(reverse("avito_crm_attribution", args=[4]), {"call": call.pk})
        self.assertEqual(denied.status_code, 403)
        PhoneCall.objects.filter(pk=call.pk).update(connection=original_connection)
        CrmSourceLink.objects.create(organization=self.foreign, avito_connection=self.goods, phone_call=call)
        with self.assertRaises(PermissionDenied):
            links.source_for(self.manager, self.connection, "call", call.pk)
        page = self.client.get(reverse("avito_crm_attribution", args=[4]))
        self.assertNotIn(call.pk, {row["id"] for row in page.context["calls"]})
        self.assertEqual(CrmSourceLink.objects.count(), 1)

    @patch("pool_service.crm_source_orders.observed")
    def test_order_reconfirmation_has_own_ack_and_history_while_quality_preserves_it(self, observed):
        guid = uuid.uuid4()
        observed.return_value = {"guid": guid, "evidence": {
            "source": "active_money_forecast_planning_snapshot", "snapshot_id": 1,
            "business_sha256": "first-business"}}
        link, _ = links.save_link(self.owner, 4, "conversation", self.dialog.uuid,
            item_id=self.item.pk, order_action="set", order_guid=str(guid), operation=uuid.uuid4())
        first_ack = dict(link.order_evidence)
        observed.return_value = {"guid": guid, "evidence": {
            "source": "active_money_forecast_planning_snapshot", "snapshot_id": 2,
            "business_sha256": "changed-business"}}
        link, _ = links.save_link(self.owner, 4, "conversation", self.dialog.uuid,
            item_id=self.item.pk, order_action="set", order_guid=str(guid), operation=uuid.uuid4(),
            expected_revision=1)
        event = DataAuditLog.objects.filter(entity_type="crm_source_link").order_by("-pk").first()
        self.assertNotEqual(event.before["order_evidence_sha256"], event.after["order_evidence_sha256"])
        self.assertEqual(event.before["order_snapshot_id"], 1)
        self.assertEqual(event.after["order_snapshot_id"], 2)
        self.assertEqual(event.after["order_confirmed_by_id"], self.owner.pk)
        financial_ack = dict(link.order_evidence)
        link, _ = self.save(expected_revision=2, quality="non_target")
        self.assertEqual(link.order_evidence, financial_ack)
        self.assertEqual(link.confirmed_by_id, self.manager.pk)
        self.assertEqual(link.order_evidence["manual_confirmed_by_id"], self.owner.pk)
        self.assertNotEqual(first_ack["business_sha256"], link.order_evidence["business_sha256"])

    def test_request_search_keeps_valid_existing_selection_when_query_matches_other_cards(self):
        self.save()
        response = self.client.get(reverse("avito_crm_attribution", args=[4]),
            {"conversation": self.dialog.uuid, "request_q": "NO-MATCH"})
        self.assertEqual(response.status_code, 200)
        choices = response.context["form"].fields["item_id"].widget.choices
        self.assertIn(str(self.item.pk), {str(value) for value, _label in choices})

    def test_binding_drift_before_connection_lock_never_uses_the_old_privacy_mutex(self):
        foreign_channel = CommunicationChannel.objects.create(organization=self.foreign, kind="avito", name="Moved")
        moved = ChannelConnection.objects.get(pk=4)
        moved.channel = foreign_channel
        OrganizationAccess.objects.create(organization=self.foreign, user=self.manager, role="manager")
        with patch("pool_service.crm_source_links.ChannelConnection.objects.select_for_update") as locked, patch(
                "pool_service.crm_source_links.Organization.objects.select_for_update") as organization_lock:
            locked.return_value.select_related.return_value.get.return_value = moved
            with self.assertRaises(PermissionDenied):
                self.save()
            organization_lock.assert_not_called()
        self.assertFalse(CrmSourceLink.objects.exists())
        self.assertFalse(DataAuditLog.objects.filter(entity_type="crm_source_link").exists())


    def test_financial_only_history_is_not_disclosed_to_a_manager(self):
        link, _ = self.save()
        DataAuditLog.objects.create(entity_type="crm_source_link", entity_id=str(link.pk),
            organization=self.org, actor=self.owner, action="update",
            changed_fields=["order_evidence_sha256", "order_snapshot_id", "revision"])
        page = self.client.get(self.target())
        self.assertEqual(page.status_code, 200)
        labels = [label for event in page.context["history"] for label in event["changed_fields"]]
        self.assertNotIn("подтверждение финансового источника", labels)
        self.assertNotIn("снимок финансового источника", labels)

    @patch("pool_service.crm_source_links.owned_ads")
    def test_ad_reconfirmation_audits_source_evidence_and_quality_preserves_its_actor(self, owned):
        owned.return_value = {"8343962135": timezone.now()}
        link, _ = self.save(ad_mode="manual", manual_ad_id="8343962135")
        self.assertEqual(link.evidence["ad_confirmed_by_id"], self.manager.pk)
        owned.return_value = {"8343962135": timezone.now()}
        link, _ = links.save_link(self.owner, 4, "conversation", self.dialog.uuid,
            item_id=self.item.pk, ad_mode="manual", manual_ad_id="8343962135",
            expected_revision=1, operation=uuid.uuid4())
        event = DataAuditLog.objects.filter(entity_type="crm_source_link").order_by("-pk").first()
        self.assertNotEqual(event.before["source_evidence_sha256"], event.after["source_evidence_sha256"])
        self.assertEqual(event.after["ad_confirmed_by_id"], self.owner.pk)
        ad_ack = dict(link.evidence)
        link, _ = self.save(expected_revision=2, quality="target")
        self.assertEqual(link.evidence, ad_ack)
        self.assertEqual(link.confirmed_by_id, self.manager.pk)
        self.assertEqual(link.evidence["ad_confirmed_by_id"], self.owner.pk)

    def test_task_with_foreign_or_unknown_call_source_is_not_an_eligible_next_step(self):
        task = ServiceTask.objects.create(organization=self.org, crm_item=self.item,
            title="PRIVATE-TASK-SOURCE", start_date=timezone.localdate(), created_by=self.manager,
            payload_json={"source_call_id": 999999})
        self.assertFalse(links.task_allowed(self.manager, task, self.item, self.org.pk))
        with self.assertRaises(PermissionDenied):
            self.save(task_id=task.pk)


@skipUnless(connection.vendor == "mysql", "Requires MySQL row-lock concurrency, not SQLite.")
class CrmSourceConcurrentOperationTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        CrmAttributionTests.setUp(self)

    def test_two_natural_database_connections_with_same_operation_create_one_link_and_audit(self):
        operation = uuid.uuid4()
        barrier = Barrier(2)

        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=20)
                result, changed = links.save_link(self.manager, 4, "conversation", self.dialog.uuid,
                    item_id=self.item.pk, quality="target", expected_revision=0, operation=operation)
                return result.pk, changed, result.revision
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run) for _ in range(2)]
            results = [future.result(timeout=40) for future in futures]
        self.assertEqual(len({row[0] for row in results}), 1)
        self.assertEqual(sorted(row[1] for row in results), [False, True])
        self.assertEqual({row[2] for row in results}, {1})
        self.assertEqual(CrmSourceLink.objects.count(), 1)
        self.assertEqual(DataAuditLog.objects.filter(entity_type="crm_source_link").count(), 1)

