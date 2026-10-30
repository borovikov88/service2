from datetime import timedelta
from pathlib import Path
import json
import os
import subprocess
from unittest import skipUnless
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone
from pool_service.models import Client,Organization,OrganizationAccess
from pool_service.call_processing_models import CallPrivateNumber


@skipUnless(os.environ.get('CONTACTS_BROWSER_TEST')=='1','Explicit browser gate')
class OwnerPrivacyBrowserTests(StaticLiveServerTestCase):
    def test_owner_only_peer_control_and_no_staff_disclosure(self):
        org=Organization.objects.create(name='Synthetic privacy browser',paid_until=timezone.now()+timedelta(days=30))
        owner=get_user_model().objects.create_user(username='synthetic-owner-browser')
        staff=get_user_model().objects.create_user(username='synthetic-staff-browser')
        OrganizationAccess.objects.create(user=owner,organization=org,role='owner')
        OrganizationAccess.objects.create(user=staff,organization=org,role='manager')
        person=Client.objects.create(name='Synthetic Peer',organization=org,client_type='private',phone='+79001112233')
        self.client.force_login(owner);owner_cookie=self.client.cookies[settings.SESSION_COOKIE_NAME].value
        self.client.logout();self.client.force_login(staff);staff_cookie=self.client.cookies[settings.SESSION_COOKIE_NAME].value
        # logout invalidated the owner's first session; create independent clients.
        from django.test import Client as HttpClient
        owner_client=HttpClient();owner_client.force_login(owner);owner_cookie=owner_client.cookies[settings.SESSION_COOKIE_NAME].value
        payload=dict(base=self.live_server_url,card=reverse('client_detail',args=[person.pk]),cookieName=settings.SESSION_COOKIE_NAME,ownerCookie=owner_cookie,staffCookie=staff_cookie)
        result=subprocess.run(['node',str(Path(__file__).with_name('owner_call_privacy.browser.cjs'))],input=json.dumps(payload),text=True,capture_output=True,timeout=90)
        self.assertEqual(result.returncode,0,(result.stdout+result.stderr)[-6000:])
        self.assertFalse(CallPrivateNumber.objects.exists())
