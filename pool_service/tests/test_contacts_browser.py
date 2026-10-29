"""Actual local Chromium, synthetic records, no external requests."""
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
from pool_service.models import Client, Organization, OrganizationAccess
from pool_service.client_crm_models import ClientBusinessRole, ClientBusinessRoleEvent, ClientCompanyLink


@skipUnless(os.environ.get("CONTACTS_BROWSER_TEST") == "1", "Explicit local Chromium gate")
class ContactsBrowserTests(StaticLiveServerTestCase):
    def test_supplier_role_search_pagination_reload_and_mobile(self):
        org=Organization.objects.create(name="Synthetic browser contacts",paid_until=timezone.now()+timedelta(days=30))
        user=get_user_model().objects.create_user(username="synthetic-contacts-browser")
        OrganizationAccess.objects.create(user=user,organization=org,role="owner")
        companies=[Client.objects.create(name=f"Synthetic Company {i:02}",client_type="legal",organization=org) for i in range(53)]
        person=Client.objects.create(name="Synthetic Linked Person",client_type="private",organization=org)
        ClientCompanyLink.objects.create(company=companies[-1],person=person,position="Synthetic role")
        self.client.force_login(user)
        data={"base":self.live_server_url,"list":reverse("clients_list"),"card":reverse("client_detail",args=[companies[-1].pk]),
            "sessionCookie":self.client.cookies[settings.SESSION_COOKIE_NAME].value,"sessionCookieName":settings.SESSION_COOKIE_NAME}
        result=subprocess.run(["node",str(Path(__file__).with_name("contacts.browser.cjs"))],input=json.dumps(data),text=True,capture_output=True,timeout=120)
        self.assertEqual(result.returncode,0,(result.stdout+result.stderr)[-8000:])
        self.assertFalse(ClientBusinessRole.objects.get(client=companies[-1]).active)
        self.assertEqual(ClientBusinessRoleEvent.objects.count(),2)
        self.assertEqual(Client.objects.count(),54)
