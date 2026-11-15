"""Real Chromium acceptance for completing a lead without a deal."""
from datetime import timedelta
import os
from pathlib import Path
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientBusinessRole, ClientCompanyLink
from pool_service.crm_commercial_models import CrmDeal, CrmLead, CrmLeadDealLink, CrmLeadTaskLink
from pool_service.models import Client, Organization, OrganizationAccess, ServiceTask
from pool_service.tests.test_crm_lead_commands import LeadFixture
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process


@skipUnless(os.environ.get("CRM_LEADS_BROWSER_TEST") == "1", "Dedicated Chromium acceptance enables this test")
class LeadCustomerCompletionBrowserTests(LeadFixture, StaticLiveServerTestCase):
    def exercise_completion(self, *, company_only=False):
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=["plan_type", "paid_until"])
        if company_only:
            self.grant.create_own_deals = False
            self.grant.save(update_fields=["create_own_deals"])
        else:
            OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="owner")
        lead = self.create(title="Synthetic customer completion request")
        task_id = lead.next_task_id
        archived = CrmLead.objects.create(organization=self.org, assignee=self.user,
            title="Synthetic archived completion request", is_archived=True)
        person_name = "Synthetic completion person " + "ОченьДлинноеИмя" * 7
        company_name = "Synthetic completion company " + "ОченьДлиннаяКомпания" * 7
        http = HttpClient(); http.force_login(self.user)
        fixture = {
            "base": self.live_server_url,
            "path": reverse("crm_live_leads") + "?org_id=" + str(self.org.pk),
            "command": reverse("crm_live_lead_command"),
            "leadId": lead.pk, "archivedId": archived.pk,
            "companyOnly": company_only,
            "personName": person_name, "companyName": company_name,
            "sessionCookie": http.cookies[settings.SESSION_COOKIE_NAME].value,
            "sessionCookieName": settings.SESSION_COOKIE_NAME,
        }
        code, output = _bounded_browser_process(
            ["node", str(Path(__file__).with_name("crm_lead_customer_completion.browser.cjs"))], fixture)
        self.assertEqual(code, 0, output)
        lead.refresh_from_db(); archived.refresh_from_db()
        self.assertEqual((lead.status, lead.result, lead.is_archived), ("closed", "customer_saved", False))
        self.assertIsNone(lead.next_task_id)
        self.assertTrue(archived.is_archived)
        self.assertEqual(archived.result, "")
        self.assertFalse(Client.objects.filter(name__icontains="cancelled").exists())
        company = Client.objects.get(name=company_name)
        self.assertEqual((lead.client_id, company.client_type), (company.pk, "legal"))
        if company_only:
            self.assertIsNone(lead.contact_id)
            self.assertEqual(Client.objects.count(), 2)
            self.assertFalse(ClientCompanyLink.objects.exists())
        else:
            person = Client.objects.get(name=person_name)
            self.assertEqual((lead.contact_id, person.client_type), (person.pk, "private"))
            self.assertEqual(Client.objects.count(), 3)
            self.assertTrue(ClientCompanyLink.objects.filter(person=person, company=company).exists())
            self.assertTrue(ClientBusinessRole.objects.filter(client=person, role="landlord", active=True).exists())
        self.assertFalse(CrmDeal.objects.exists())
        self.assertFalse(CrmLeadDealLink.objects.exists())
        self.assertEqual(ServiceTask.objects.count(), 1)
        self.assertEqual(CrmLeadTaskLink.objects.filter(lead=lead).count(), 1)
        task = ServiceTask.objects.get(pk=task_id)
        self.assertTrue(task.is_archived)
        self.assertEqual(task.status, ServiceTask.STATUS_CANCELLED)

    def test_choose_cancel_then_mobile_contact_and_company_completion_with_saved_links(self):
        self.exercise_completion()

    def test_company_only_completion_does_not_require_deal_creation_permission(self):
        self.exercise_completion(company_only=True)
