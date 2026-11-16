"""Real card commands, source permissions and chronological pagination."""
from datetime import timedelta
from uuid import uuid4
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_commercial_models import CrmCardComment, CrmCommand, CrmDeal, CrmEvent, CrmLeadDealLink
from pool_service.models import Organization, OrganizationAccess, ServiceTask
from pool_service.services.crm_commercial_commands import create_commercial_deal, execute_commercial_command, CommercialConflict
from pool_service.tests.test_crm_lead_commands import LeadFixture


class CardActivityTests(LeadFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save()
        self.grant.view_team_deals = self.grant.edit_own_deals = True
        self.grant.save()
        self.lead = self.create(client_id=self.client.pk)
        result = create_commercial_deal(actor=self.user, organization=self.org, operation_key="activity-deal",
            client_id=self.client.pk, title="Synthetic activity deal", pipeline_id=self.pipeline.pk, next_action=self.action())
        self.deal_card = CrmDeal.objects.get(pk=result["deal_id"])
        self.http = HttpClient()
        self.http.force_login(self.user)

    def url(self, subject, **params):
        from urllib.parse import urlencode
        kind = "lead" if subject._meta.model_name == "crmlead" else "deal"
        return reverse("crm_" + kind + "_activity", args=[subject.pk]) + "?" + urlencode({"org_id": self.org.pk, **params})

    def comment(self, subject, body="Synthetic internal comment", key=None, revision=None):
        if subject._meta.model_name == "crmlead":
            return self.command("lead.comment", {"body": body}, subject, key=key,
                expected_revision=subject.revision if revision is None else revision)
        return execute_commercial_command(actor=self.user, organization=self.org, operation_key=key or uuid4().hex,
            command_type="deal.comment", subject_id=subject.pk,
            expected_revision=subject.revision if revision is None else revision, changes={"body": body})

    def test_comments_persist_once_and_leave_tasks_and_accounting_unchanged(self):
        tasks = list(ServiceTask.objects.values())
        for subject in (self.lead, self.deal_card):
            key = "comment-" + subject._meta.model_name
            original = subject.revision
            result = self.comment(subject, "Synthetic <script>plain text</script>\nSecond line", key=key)
            self.assertEqual(result, self.comment(subject, "Synthetic <script>plain text</script>\nSecond line", key=key))
            subject.refresh_from_db()
            self.assertEqual(subject.revision, original + 1)
            page = self.http.get(self.url(subject, type="comment"))
            self.assertEqual(page.status_code, 200)
            self.assertEqual(page.json()["items"][0]["body"], "Synthetic <script>plain text</script>\nSecond line")
            self.assertEqual(page.json()["items"][0]["actor"], self.user.username)
            with self.assertRaises(CommercialConflict):
                self.comment(subject, "Different body", key=key, revision=original)
        self.assertEqual(CrmCardComment.objects.count(), 2)
        self.assertEqual(list(ServiceTask.objects.values()), tasks)
        self.deal_card.refresh_from_db()
        self.assertIsNone(self.deal_card.amount)
        self.assertEqual(self.deal_card.outcome, "open")
        self.assertNotIn("body", CrmEvent.objects.order_by("-pk").first().safe_diff)

    def test_comments_require_current_ownership_and_rights_even_on_replay(self):
        self.comment(self.deal_card, key="old-comment")
        self.grant.edit_own_deals = False
        self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.comment(self.deal_card, key="old-comment")
        stranger = get_user_model().objects.create_user("synthetic-other-card-owner")
        OrganizationAccess.objects.create(organization=self.org, user=stranger, role="manager")
        self.lead.assignee = stranger
        self.lead.save()
        with self.assertRaises(PermissionDenied):
            self.comment(self.lead)
        self.assertEqual(CrmCardComment.objects.count(), 1)

    def test_invalid_comments_and_stale_revision_do_not_write(self):
        before = (CrmEvent.objects.count(), CrmCommand.objects.count())
        for subject in (self.lead, self.deal_card):
            for body in ("  ", "x" * 4001, None):
                with self.assertRaises(ValidationError):
                    self.comment(subject, body)
            with self.assertRaises(CommercialConflict):
                self.comment(subject, revision=subject.revision + 5)
        self.assertEqual((CrmEvent.objects.count(), CrmCommand.objects.count()), before)
        self.assertFalse(CrmCardComment.objects.exists())

    def test_budget_only_edit_preserves_private_next_task_and_records_before_after(self):
        other = get_user_model().objects.create_user("synthetic-private-task-owner")
        OrganizationAccess.objects.create(organization=self.org, user=other, role="manager")
        task = self.lead.next_task
        task.created_by = other
        task.primary_responsible = other
        task.save()
        task.responsibles.set([other])
        before = list(ServiceTask.objects.values())
        self.command("lead.edit", {"budget_kind": "exact", "budget_min": "12500.50", "budget_max": "12500.50", "currency": "RUB"}, self.lead)
        self.lead.refresh_from_db()
        self.assertEqual(str(self.lead.budget_min), "12500.50")
        diff = CrmEvent.objects.filter(lead=self.lead).latest("pk").safe_diff
        self.assertEqual(diff["budget_min"], {"before": None, "after": "12500.50"})
        self.assertNotIn("need_changed", diff)
        self.assertEqual(list(ServiceTask.objects.values()), before)
        page = self.http.get(self.url(self.lead)).json()
        self.assertTrue(any("12\u00a0500,50" in " ".join(item.get("details", [])) for item in page["items"]))
        self.assertFalse(any(item["kind"] == "task" for item in page["items"]))

    def test_comment_on_completed_card_does_not_reopen_or_acknowledge(self):
        self.lead.status, self.lead.result, self.lead.needs_review = "closed", "no_deal", True
        self.lead.reason = "Synthetic completed review"
        self.lead.save()
        self.deal_card.outcome, self.deal_card.closed_at = "lost", timezone.now()
        self.deal_card.close_reason = "Synthetic rejection"
        self.deal_card.save()
        self.comment(self.lead)
        self.comment(self.deal_card)
        self.lead.refresh_from_db()
        self.deal_card.refresh_from_db()
        self.assertEqual((self.lead.status, self.lead.needs_review, self.deal_card.outcome), ("closed", True, "lost"))

    def test_cursor_is_scoped_and_every_same_time_event_is_available(self):
        stamp = timezone.now()
        for index in range(65):
            CrmEvent.objects.create(organization=self.org, deal=self.deal_card, event_type="deal.edit", occurred_at=stamp)
        url = self.url(self.deal_card, type="change")
        first = self.http.get(url).json()
        self.assertEqual(len(first["items"]), 30)
        second = self.http.get(self.url(self.deal_card, type="change", cursor=first["cursor"])).json()
        third = self.http.get(self.url(self.deal_card, type="change", cursor=second["cursor"])).json()
        identifiers = [item["id"] for page in (first, second, third) for item in page["items"]]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(len(identifiers), 66)
        self.assertIsNone(third["cursor"])
        self.assertEqual(self.http.get(self.url(self.lead, type="change", cursor=first["cursor"])).status_code, 404)
        self.assertEqual(self.http.get(self.url(self.deal_card, type="comment", cursor=first["cursor"])).status_code, 404)

    def test_converted_deal_contains_readable_lead_comments_only(self):
        result = self.comment(self.lead, "Synthetic qualification context")
        CrmLeadDealLink.objects.create(organization=self.org, lead=self.lead, deal=self.deal_card,
            command=CrmCommand.objects.get(events__pk=result["event_id"]), kind="existing")
        items = self.http.get(self.url(self.deal_card, type="comment")).json()["items"]
        self.assertEqual(items[0]["body"], "Synthetic qualification context")
        self.assertEqual(items[0]["origin"], "Из исходного лида")
        self.grant.view_leads = False
        self.grant.save()
        self.assertEqual(self.http.get(self.url(self.deal_card, type="comment")).json()["items"], [])

    def test_activity_get_is_read_only_and_foreign_scope_is_denied(self):
        before = (CrmCommand.objects.count(), CrmEvent.objects.count(), list(ServiceTask.objects.values()))
        self.assertEqual(self.http.get(self.url(self.deal_card)).status_code, 200)
        self.assertEqual(self.http.get(self.url(self.lead)).status_code, 200)
        self.assertEqual(before, (CrmCommand.objects.count(), CrmEvent.objects.count(), list(ServiceTask.objects.values())))
        foreign = Organization.objects.create(name="Synthetic foreign activity", crm_v2_read=True)
        self.assertEqual(self.http.get(self.url(self.deal_card, org_id=foreign.pk)).status_code, 403)

    def test_http_comment_uses_csrf_and_command_boundary(self):
        import json
        url = reverse("crm_live_command") + "?org_id=" + str(self.org.pk)
        payload = {"operation_key": "http-comment", "command_type": "deal.comment", "subject_id": self.deal_card.pk,
            "expected_revision": self.deal_card.revision, "changes": {"body": "Synthetic HTTP comment"}}
        protected = HttpClient(enforce_csrf_checks=True)
        protected.force_login(self.user)
        self.assertEqual(protected.post(url, data=json.dumps(payload), content_type="application/json").status_code, 403)
        response = self.http.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(CrmCardComment.objects.get().body, "Synthetic HTTP comment")

    def communications(self):
        from pool_service.communication_models import CommunicationAccess, CommunicationChannel, ChannelConnection, Conversation, ConversationMessage, TelephonyConnection, PhoneCall
        CommunicationAccess.objects.update_or_create(organization=self.org, user=self.user, defaults={
            "can_view_conversations": True, "can_view_all_calls": True, "can_view_own_calls": True})
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Synthetic channel")
        connection = ChannelConnection.objects.create(channel=channel, external_id="synthetic-account", name="Synthetic connection")
        conversation = Conversation.objects.create(organization=self.org, connection=connection, external_id="synthetic-chat", client=self.client)
        incoming = ConversationMessage.objects.create(conversation=conversation, direction="in", body="Synthetic incoming content", sender_name="Synthetic customer")
        outgoing = ConversationMessage.objects.create(conversation=conversation, direction="out", body="Synthetic reply content", sent_by=self.user, delivery_status="delivered")
        telephone = TelephonyConnection.objects.create(organization=self.org, external_id="synthetic-activity-line")
        call = PhoneCall.objects.create(organization=self.org, connection=telephone, client=self.client,
            employee=self.user, external_id="synthetic-activity-call", phone_number="+79005550001", direction="in",
            started_at=timezone.now(), result="missed")
        return incoming, outgoing, call

    def test_calls_and_both_message_directions_use_real_links_not_phone_matching(self):
        from pool_service.communication_models import ConversationReadState
        incoming, outgoing, call = self.communications()
        before = (CrmEvent.objects.count(), ConversationReadState.objects.count())
        items = self.http.get(self.url(self.deal_card)).json()["items"]
        ids = {item["id"] for item in items}
        self.assertTrue({"message:" + str(incoming.pk), "message:" + str(outgoing.pk), "call:" + str(call.pk)} <= ids)
        self.assertEqual(next(item for item in items if item["kind"] == "call")["title"], "Пропущенный звонок")
        self.assertEqual(before, (CrmEvent.objects.count(), ConversationReadState.objects.count()))
        call.client = None
        call.save()
        self.assertEqual(self.http.get(self.url(self.deal_card, type="call")).json()["items"], [])

    def test_current_communication_rights_and_private_call_are_respected(self):
        from pool_service.communication_models import CommunicationAccess
        from pool_service.call_processing_models import CallPrivateNumber
        from pool_service.phone_utils import normalize_phone
        incoming, outgoing, call = self.communications()
        CallPrivateNumber.objects.create(organization=self.org, owner=self.user, phone_key=normalize_phone(call.phone_number))
        self.assertEqual(self.http.get(self.url(self.deal_card, type="call")).json()["items"], [])
        CommunicationAccess.objects.filter(organization=self.org, user=self.user).update(can_view_conversations=False)
        self.assertEqual(self.http.get(self.url(self.deal_card, type="message")).json()["items"], [])

    def test_client_linked_call_becoming_private_during_read_is_fully_hidden(self):
        from pool_service.call_processing_models import CallPrivateNumber
        from pool_service.communication_views import _authorize_call_access
        from pool_service.phone_utils import normalize_phone
        _, _, call = self.communications()

        def make_private():
            CallPrivateNumber.objects.get_or_create(organization=self.org, owner=self.user,
                phone_key=normalize_phone(call.phone_number))

        for phase in ("before_authorization", "after_authorization"):
            with self.subTest(phase=phase):
                CallPrivateNumber.objects.all().delete()

                def authorize(request, current_call):
                    if phase == "before_authorization":
                        make_private()
                    try:
                        return _authorize_call_access(request, current_call)
                    finally:
                        if phase == "after_authorization":
                            make_private()

                with patch("pool_service.communication_views._authorize_call_access", side_effect=authorize) as checked:
                    response = self.http.get(self.url(self.deal_card, type="call"))
                checked.assert_called_once()
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["items"], [])

    def test_mixed_stream_cursor_does_not_skip_equal_timestamps(self):
        from pool_service.communication_models import ConversationMessage
        incoming, outgoing, call = self.communications()
        stamp = timezone.now()
        for index in range(35):
            CrmEvent.objects.create(organization=self.org, deal=self.deal_card, event_type="deal.edit", occurred_at=stamp)
            ConversationMessage.objects.create(conversation=incoming.conversation, direction="in", body="Synthetic paginated " + str(index), intake_occurred_at=stamp)
        identifiers, cursor = [], None
        for _ in range(4):
            page = self.http.get(self.url(self.deal_card, **({"cursor": cursor} if cursor else {}))).json()
            identifiers += [item["id"] for item in page["items"]]
            cursor = page["cursor"]
            if not cursor:
                break
        self.assertIsNone(cursor)
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertEqual(sum(item.startswith("message:") for item in identifiers), 37)
        self.assertEqual(sum(item.startswith("event:") for item in identifiers), 36)
