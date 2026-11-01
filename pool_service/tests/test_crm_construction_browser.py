import os
from pathlib import Path
from unittest import skipUnless
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from pool_service.tests.test_crm_construction import ConstructionFixture
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process
from pool_service.crm_construction_models import CrmConstructionLaunch
from pool_service.models import ServiceTask


@skipUnless(os.environ.get('CRM_LEADS_BROWSER_TEST')=='1','Dedicated Chromium acceptance')
class ConstructionBrowserTests(ConstructionFixture,StaticLiveServerTestCase):
    def test_cancel_launch_replay_mobile_keyboard(self):
        fixture=dict(base=self.live_server_url,path=self.launch_url+'&project='+str(self.item.pk),
            task=self.task.pk,recipient=self.recipient.pk,senderCookie=self.http.cookies[settings.SESSION_COOKIE_NAME].value,
            sessionCookieName=settings.SESSION_COOKIE_NAME)
        code,output=_bounded_browser_process(['node',str(Path(__file__).with_name('crm_construction.browser.cjs'))],fixture)
        self.assertEqual(code,0,output)
        self.assertEqual(CrmConstructionLaunch.objects.count(),1);self.assertEqual(ServiceTask.objects.count(),1)

    def test_no_authority_denies_launch_ui(self):
        self.authority.delete()
        fixture=dict(base=self.live_server_url,path=self.launch_url,denied=True,
            senderCookie=self.http.cookies[settings.SESSION_COOKIE_NAME].value,sessionCookieName=settings.SESSION_COOKIE_NAME)
        code,output=_bounded_browser_process(['node',str(Path(__file__).with_name('crm_construction.browser.cjs'))],fixture)
        self.assertEqual(code,0,output);self.assertEqual(CrmConstructionLaunch.objects.count(),0)
