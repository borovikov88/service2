"""Real-browser acceptance for the same editor in Contacts, Deals and Leads."""
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
from pool_service.crm_commercial_models import CrmDeal
from pool_service.models import Client, Organization, OrganizationAccess, ServiceTask
from pool_service.tests.test_crm_lead_commands import LeadFixture
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process


@skipUnless(os.environ.get("CONTACTS_BROWSER_TEST") == "1", "Dedicated Chromium acceptance enables this test")
class ContactCreationBrowserTests(LeadFixture, StaticLiveServerTestCase):
    def test_shared_directory_deal_and_lead_editor_cancel_create_and_saved_links(self):
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=["plan_type", "paid_until"])
        OrganizationAccess.objects.filter(user=self.user, organization=self.org).update(role="owner")
        self.grant.view_team_deals = True
        self.grant.save(update_fields=["view_team_deals"])
        lead = self.create()
        lead_task_id = lead.next_task_id
        http = HttpClient(); http.force_login(self.user)
        bootstrap = http.get(reverse("client_create"), HTTP_ACCEPT="application/json").json()["bootstrap"]
        self.assertTrue(bootstrap["can_categorize_contacts"])
        scope = "?org_id=" + str(self.org.pk)
        fixture = {"base": self.live_server_url, "orgId": self.org.pk, "leadId": lead.pk, "actorId": self.user.pk,
            "responsiblesPath": reverse("crm_assignment_candidates"),
            "directory": reverse("clients_list"), "createPath": reverse("client_create"),
            "lookup": reverse("contact_create_lookup") + scope,
            "deals": reverse("crm_live_workspace") + scope,
            "leads": reverse("crm_live_leads") + scope,
            "dealCommandPath": reverse("crm_live_command"), "leadCommandPath": reverse("crm_live_lead_command"),
            "sessionCookie": http.cookies[settings.SESSION_COOKIE_NAME].value,
            "sessionCookieName": settings.SESSION_COOKIE_NAME}
        code, output = _bounded_browser_process(["node", str(Path(__file__).with_name("contact_creation.browser.cjs"))], fixture)
        self.assertEqual(code, 0, output)
        self.assertFalse(Client.objects.filter(name__icontains="cancelled").exists())
        self.assertEqual(Client.objects.count(), 8)
        for prefix in ("Directory", "Deal", "Lead"):
            person = Client.objects.get(name=f"Synthetic {prefix} person")
            company = Client.objects.get(name=f"Synthetic {prefix} company")
            self.assertEqual((person.organization_id, company.organization_id), (self.org.pk, self.org.pk))
            self.assertEqual((person.client_type, company.client_type), ("private", "legal"))
            self.assertTrue(ClientCompanyLink.objects.filter(person=person, company=company).exists())
            self.assertTrue(ClientBusinessRole.objects.filter(client=person, role="landlord", active=True).exists())
        company_only = Client.objects.get(name="Synthetic directory company only")
        self.assertEqual(company_only.client_type, "legal")
        self.assertFalse(ClientCompanyLink.objects.filter(company=company_only).exists())
        lead.refresh_from_db()
        self.assertEqual(lead.result, "qualified")
        self.assertEqual(lead.contact.name, "Synthetic Lead person")
        self.assertEqual(lead.client.name, "Synthetic Lead company")
        self.assertEqual(CrmDeal.objects.count(), 2)
        converted = CrmDeal.objects.get(contact=lead.contact)
        self.assertEqual(converted.next_action.task_link.task_id, lead_task_id)
        direct = CrmDeal.objects.get(title="Synthetic unified deal")
        self.assertEqual((direct.contact.name, direct.client.name), ("Synthetic Deal person", "Synthetic Deal company"))
        self.assertEqual(ServiceTask.objects.count(), 2)
