from dataclasses import replace
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch
import json,os,subprocess
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone
from pool_service.tests.test_crm_service_bindings import BindingFixture
from pool_service.services import crm_service_binding_reader as reader
from pool_service.crm_service_binding_models import ServiceAllocationVersion


@skipUnless(os.environ.get('CONTACTS_BROWSER_TEST')=='1','Explicit browser gate')
class BindingBrowserTests(BindingFixture,StaticLiveServerTestCase):
    def test_fresh_read_changed_source_save_replay_navigation_mobile(self):
        self.client.force_login(self.actor)
        payload=dict(base=self.live_server_url,url=reverse('crm_service_bindings',args=[self.deal.pk])+'?org_id='+str(self.org.pk),
            cookieName=settings.SESSION_COOKIE_NAME,cookie=self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            refs={key:getattr(self.snapshot,key) for key in ('document_ref','organization_ref','client_ref','contract_ref')})
        calls=[]
        def read(**kwargs):
            calls.append(1)
            return replace(self.snapshot,observed_at=timezone.now().isoformat(),snapshot_hash=self.snapshot.snapshot_hash if len(calls)==1 else 'e'*64)
        with self.settings(ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_USER_IDS=(self.actor.pk,)),patch.object(reader,'read_document',side_effect=read):
            result=subprocess.run(['node',str(Path(__file__).with_name('crm_service_binding.browser.cjs'))],input=json.dumps(payload),text=True,capture_output=True,timeout=100)
        self.assertEqual(result.returncode,0,(result.stdout+result.stderr)[-5000:])
        self.assertEqual(ServiceAllocationVersion.objects.count(),2)
        self.assertGreaterEqual(len(calls),4)
