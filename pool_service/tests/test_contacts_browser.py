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
            "personCard":reverse("client_detail",args=[person.pk]), "userId":user.pk, "sessionCookie":self.client.cookies[settings.SESSION_COOKIE_NAME].value,"sessionCookieName":settings.SESSION_COOKIE_NAME}
        from unittest.mock import patch
        from pool_service.services import contact_people
        original = contact_people.save_phone
        failures = []
        def diagnosed(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except Exception as error:
                failures.append({"reason":str(error),"type":kwargs.get("phone_type"),"primary":kwargs.get("primary"),"revision_length":len(kwargs.get("revision") or "")})
                raise
        with patch.object(contact_people, "save_phone", diagnosed):
            result=subprocess.run(["node",str(Path(__file__).with_name("contacts.browser.cjs"))],input=json.dumps(data),text=True,capture_output=True,timeout=120)
        self.assertEqual(result.returncode,0,(result.stdout+result.stderr)[-8000:] + str(failures))
        self.assertFalse(ClientBusinessRole.objects.get(client=companies[-1]).active)
        self.assertEqual(ClientBusinessRoleEvent.objects.count(),2)
        self.assertEqual(Client.objects.count(),54)
