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
