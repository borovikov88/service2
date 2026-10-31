import os
from pathlib import Path
from unittest import skipUnless
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client
from pool_service.tests.test_crm_handoffs import HandoffFixture
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process
from pool_service.crm_handoff_models import CrmHandoff, CrmHandoffEvent
from pool_service.models import ServiceTask


@skipUnless(os.environ.get('CRM_LEADS_BROWSER_TEST')=='1','Dedicated local Chromium acceptance')
class HandoffBrowserTests(HandoffFixture,StaticLiveServerTestCase):
    def test_preview_cancel_submit_recipient_accept_mobile(self):
        recipient=Client();recipient.force_login(self.recipient)
        fixture=dict(base=self.live_server_url,path=self.handoff_url+'&item='+str(self.item.pk),task=self.task.pk,
            senderCookie=self.http.cookies[settings.SESSION_COOKIE_NAME].value,
            recipientCookie=recipient.cookies[settings.SESSION_COOKIE_NAME].value,
            sessionCookieName=settings.SESSION_COOKIE_NAME)
        code,output=_bounded_browser_process(['node',str(Path(__file__).with_name('crm_handoff.browser.cjs'))],fixture)
        self.assertEqual(code,0,output)
        self.assertEqual(CrmHandoff.objects.get().status,'accepted')
        self.assertEqual(list(CrmHandoffEvent.objects.order_by('pk').values_list('action',flat=True)),['submit','accept'])
        self.assertEqual(ServiceTask.objects.count(),1)
