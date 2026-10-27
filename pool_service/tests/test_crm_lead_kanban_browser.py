"""Actual Chromium acceptance; run explicitly in the pinned CI browser gate."""
from datetime import timedelta
import json
import os
from pathlib import Path
import subprocess
from unittest import skipUnless

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_commercial_models import CrmAccess, CrmLead, CrmLeadTaskLink
from pool_service.models import Employee, Organization, OrganizationAccess, ServiceTask


@skipUnless(os.environ.get("CRM_LEADS_BROWSER_TEST") == "1", "Dedicated Chromium acceptance step enables this test")
class LeadKanbanBrowserTests(StaticLiveServerTestCase):
    def test_drag_persistence_replay_restoration_search_mobile_keyboard(self):
        org = Organization.objects.create(name="Synthetic kanban", crm_v2_read=True, crm_v2_write=True,
            plan_type=Organization.PLAN_COMPANY_PAID, paid_until=timezone.now() + timedelta(days=30))
        user = get_user_model().objects.create_user("synthetic-browser-lead")
        OrganizationAccess.objects.create(organization=org, user=user, role="manager")
        Employee.objects.create(organization=org, user=user, display_name="Synthetic manager")
        CrmAccess.objects.create(organization=org, user=user, view_leads=True, take_leads=True,
            transition_own_leads=True, create_own_leads=True, edit_own_leads=True)
        lead = CrmLead.objects.create(organization=org, title="Synthetic browser inquiry", needs_review=True)
        self.client.force_login(user)
        fixture = {"base": self.live_server_url, "workspace": reverse("crm_live_leads") + "?org_id=" + str(org.pk),
            "command": reverse("crm_live_lead_command"), "leadId": lead.pk,
            "sessionCookie": self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            "sessionCookieName": settings.SESSION_COOKIE_NAME}
        result = subprocess.run(["node", str(Path(__file__).with_name("crm_lead_kanban.browser.cjs"))],
            input=json.dumps(fixture), text=True, capture_output=True, timeout=150, check=False)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-9000:])
        lead.refresh_from_db()
        self.assertEqual((lead.status, lead.result, lead.is_archived, lead.needs_review), ("working", "", False, True))
        self.assertEqual(CrmLeadTaskLink.objects.filter(lead=lead).count(), 2)
        self.assertEqual(ServiceTask.objects.filter(is_archived=False).count(), 0)
