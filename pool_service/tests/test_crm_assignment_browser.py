"""Real Chromium checks for explicit CRM assignment, with synthetic users only."""
from datetime import timedelta
import os
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_commercial_models import CrmAccess, CrmDeal, CrmLead, CrmEvent
from pool_service.models import Employee, Notification, Organization, OrganizationAccess, ServiceTask
from pool_service.tests.test_crm_lead_commands import LeadFixture
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process


@skipUnless(os.environ.get("CRM_CARD_BROWSER_TEST") == "1", "Dedicated Chromium acceptance enables this test")
class CrmAssignmentBrowserTests(LeadFixture, StaticLiveServerTestCase):
    def test_card_amount_comments_communications_and_mobile(self):
        from pool_service.services.crm_commercial_commands import create_commercial_deal
        from pool_service.crm_commercial_models import CrmCardComment
        from pool_service.communication_models import CommunicationAccess, CommunicationChannel, ChannelConnection, Conversation, ConversationMessage, TelephonyConnection, PhoneCall
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save()
        self.grant.view_team_deals = self.grant.edit_own_deals = True
        self.grant.save()
        lead = self.create(title="Обсудить обслуживание бассейна", client_id=self.client.pk)
        result = create_commercial_deal(actor=self.user, organization=self.org, operation_key="activity-browser-deal",
            client_id=self.client.pk, pipeline_id=self.pipeline.pk, title="Обслуживание бассейна · тест", next_action=self.action())
        deal = CrmDeal.objects.get(pk=result["deal_id"])
        CommunicationAccess.objects.update_or_create(organization=self.org, user=self.user, defaults={"can_view_conversations": True, "can_view_all_calls": True})
        channel = CommunicationChannel.objects.create(organization=self.org, kind="avito", name="Synthetic channel")
        connection = ChannelConnection.objects.create(channel=channel, name="Synthetic connection", external_id="synthetic-browser-account")
        conversation = Conversation.objects.create(organization=self.org, connection=connection, external_id="synthetic-browser-chat", client=self.client)
        ConversationMessage.objects.create(conversation=conversation, direction="in", body="Synthetic incoming: обсудим график обслуживания.", sender_name="Тестовый клиент")
        ConversationMessage.objects.create(conversation=conversation, direction="out", body="Synthetic outgoing: подготовим предложение.", sent_by=self.user, delivery_status="delivered")
        telephone = TelephonyConnection.objects.create(organization=self.org, external_id="synthetic-browser-phone")
        PhoneCall.objects.create(organization=self.org, connection=telephone, client=self.client, employee=self.user,
            external_id="synthetic-browser-call", phone_number="+79005550001", direction="in", started_at=timezone.now(), result="missed")
        http = HttpClient(); http.force_login(self.user)
        fixture = dict(base=self.live_server_url,
            dealUrl=reverse("crm_live_workspace") + "?org_id=" + str(self.org.pk) + "&pipeline=" + str(self.pipeline.pk) + "&deal=" + str(deal.pk),
            leadUrl=reverse("crm_live_leads") + "?org_id=" + str(self.org.pk) + "&lead=" + str(lead.pk),
            dealCommand=reverse("crm_live_command"), leadCommand=reverse("crm_live_lead_command"),
            screenshotDir=os.environ.get("CRM_ACTIVITY_SCREENSHOT_DIR"),
            sessionCookie=http.cookies[settings.SESSION_COOKIE_NAME].value, sessionCookieName=settings.SESSION_COOKIE_NAME)
        with patch("pool_service.services.crm_assignment_notifications.send_push_to_users"), patch("pool_service.services.notifications.send_push_to_users"), patch("pool_service.services.push_notifications.send_push_to_users"):
            code, output = _bounded_browser_process(["node", str(Path(__file__).with_name("crm_card_activity.browser.cjs"))], fixture)
        self.assertEqual(code, 0, output)
        lead.refresh_from_db(); deal.refresh_from_db()
        self.assertEqual(str(deal.amount), "125000.50")
        self.assertEqual(deal.amount_basis, "agreed")
        self.assertEqual(str(lead.budget_min), "35000.75")
        self.assertEqual(lead.budget_kind, "exact")
        self.assertEqual(CrmCardComment.objects.count(), 4)
        self.assertEqual(CrmEvent.objects.filter(event_type="deal.comment").count(), 3)
        self.assertEqual(CrmEvent.objects.filter(event_type="lead.comment").count(), 1)

    def test_bulk_selection_stages_responsibles_partial_errors_and_retry(self):
        from pool_service.services.crm_commercial_commands import create_commercial_deal
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.crm_close = True
        self.org.save()
        OrganizationAccess.objects.filter(organization=self.org, user=self.user).update(role="owner")
        self.grant.view_team_deals = self.grant.edit_own_deals = self.grant.transition_own_deals = True
        self.grant.save()
        recipient = get_user_model().objects.create_user("synthetic-bulk-recipient", first_name="Сотрудник", last_name="Массовое назначение")
        OrganizationAccess.objects.create(organization=self.org, user=recipient, role="manager")
        Employee.objects.create(organization=self.org, user=recipient, display_name="Synthetic bulk recipient")
        CrmAccess.objects.create(organization=self.org, user=recipient, view_leads=True, edit_own_leads=True,
            take_leads=True, transition_own_leads=True, view_team_deals=True, edit_own_deals=True,
            transition_own_deals=True, manage_own_next_action=True)
        leads = [self.create(title="Synthetic bulk lead " + str(index)) for index in range(3)]
        deals = []
        for index in range(3):
            result = create_commercial_deal(actor=self.user, organization=self.org, operation_key="bulk-deal-" + str(index),
                client_id=self.client.pk, pipeline_id=self.pipeline.pk, title="Synthetic bulk deal " + str(index),
                next_action={"title": "Synthetic bulk next step", "due_at": self.due})
            deals.append(CrmDeal.objects.get(pk=result["deal_id"]))
        http = HttpClient(); http.force_login(self.user)
        receiver_http = HttpClient(); receiver_http.force_login(recipient)
        fixture = dict(base=self.live_server_url, org=self.org.pk,
            leads=reverse("crm_live_leads") + "?org_id=" + str(self.org.pk),
            deals=reverse("crm_live_workspace") + "?org_id=" + str(self.org.pk) + "&pipeline=" + str(self.pipeline.pk),
            leadIds=[row.pk for row in leads], dealIds=[row.pk for row in deals], recipient=recipient.pk,
            technicalStage=self.pipeline.stages.get(code="technical").pk, recipientCookie=receiver_http.cookies[settings.SESSION_COOKIE_NAME].value,
            screenshotDir=os.environ.get("CRM_BULK_SCREENSHOT_DIR"),
            sessionCookie=http.cookies[settings.SESSION_COOKIE_NAME].value, sessionCookieName=settings.SESSION_COOKIE_NAME)
        with patch("pool_service.services.crm_assignment_notifications.send_push_to_users"), patch("pool_service.services.notifications.send_push_to_users"), patch("pool_service.services.push_notifications.send_push_to_users"):
            code, output = _bounded_browser_process(["node", str(Path(__file__).with_name("crm_bulk.browser.cjs"))], fixture)
        self.assertEqual(code, 0, output)
        for lead in leads[:2]:
            lead.refresh_from_db()
            self.assertEqual((lead.assignee_id, lead.result), (recipient.pk, "lost"))
        for deal in deals[:2]:
            deal.refresh_from_db()
            self.assertEqual((deal.manager_id, deal.outcome), (recipient.pk, "lost"))
        leads[2].refresh_from_db(); deals[2].refresh_from_db()
        self.assertEqual(leads[2].assignee_id, self.user.pk)
        self.assertEqual(deals[2].manager_id, self.user.pk)
        self.assertEqual(deals[2].outcome, "open")
        self.assertEqual(CrmEvent.objects.filter(event_type="deal.assign").count(), 2)
        self.assertEqual(CrmEvent.objects.filter(event_type="lead.assign").count(), 2)
        self.assertEqual(Notification.objects.filter(user=recipient, kind="crm_assignment").count(), 4)

    def test_create_change_cancel_notifications_and_mobile(self):
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=["plan_type", "paid_until"])
        OrganizationAccess.objects.filter(organization=self.org, user=self.user).update(role="owner")
        self.grant.view_team_deals = self.grant.edit_own_deals = True
        self.grant.save(update_fields=["view_team_deals", "edit_own_deals"])
        recipients = []
        for index in (1, 2):
            user = get_user_model().objects.create_user("synthetic-assignee-" + str(index),
                first_name="Сотрудник", last_name="Назначение " + str(index))
            OrganizationAccess.objects.create(organization=self.org, user=user, role="manager")
            Employee.objects.create(organization=self.org, user=user, display_name="Synthetic assignee " + str(index))
            CrmAccess.objects.create(organization=self.org, user=user, view_leads=True,
                edit_own_leads=True, create_own_leads=True, take_leads=True, transition_own_leads=True,
                qualify_own_leads=True, view_team_deals=True, create_own_deals=True, edit_own_deals=True,
                transition_own_deals=True, manage_own_next_action=True)
            recipients.append(user)
        own = self.create(title="Synthetic lead for conversion selector")
        http = HttpClient(); http.force_login(self.user)
        receiver_http = HttpClient(); receiver_http.force_login(recipients[1])
        fixture = dict(base=self.live_server_url, org=self.org.pk,
            leads=reverse("crm_live_leads") + "?org_id=" + str(self.org.pk),
            deals=reverse("crm_live_workspace") + "?org_id=" + str(self.org.pk),
            ownLead=own.pk, actor=self.user.pk, recipients=[user.pk for user in recipients],
            names=[user.get_full_name() for user in recipients], clientName=self.client.name,
            recipientCookie=receiver_http.cookies[settings.SESSION_COOKIE_NAME].value,
            sessionCookie=http.cookies[settings.SESSION_COOKIE_NAME].value,
            sessionCookieName=settings.SESSION_COOKIE_NAME)
        with patch("pool_service.services.push_notifications.send_push_to_users"), patch("pool_service.services.notifications.send_push_to_users"), patch("pool_service.services.crm_assignment_notifications.send_push_to_users"):
            code, output = _bounded_browser_process(
                ["node", str(Path(__file__).with_name("crm_assignment.browser.cjs"))], fixture)
        self.assertEqual(code, 0, output)
        lead = CrmLead.objects.get(title="Synthetic assigned lead")
        deal = CrmDeal.objects.get(title="Synthetic assigned deal")
        self.assertEqual(lead.assignee_id, recipients[1].pk)
        self.assertEqual(deal.manager_id, recipients[1].pk)
        self.assertEqual(ServiceTask.objects.get(pk=lead.next_task_id).primary_responsible_id, recipients[0].pk)
        self.assertEqual(deal.next_action.task_link.task.primary_responsible_id, recipients[1].pk)
        self.assertEqual(ServiceTask.objects.get(pk=own.next_task_id).primary_responsible_id, self.user.pk)
        self.assertEqual(CrmEvent.objects.filter(event_type="lead.assign", lead=lead).count(), 1)
        self.assertEqual(CrmEvent.objects.filter(event_type="deal.assign", deal=deal).count(), 1)
        for recipient in recipients:
            self.assertEqual(Notification.objects.filter(user=recipient, kind="crm_assignment").count(), 2)
