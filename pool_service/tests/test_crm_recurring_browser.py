import os
from pathlib import Path
from unittest import skipUnless
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from pool_service.tests.test_crm_recurring_confirmed import ConfirmedFixture
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process
from pool_service.crm_recurring_models import CrmRecurringDraft,CrmRecurringRevision,CrmRecurringConfirmation


@skipUnless(os.environ.get('CRM_LEADS_BROWSER_TEST')=='1','Dedicated Chromium acceptance')
class RecurringBrowserTests(ConfirmedFixture,StaticLiveServerTestCase):
    def test_draft_confirmation_cancel_repeat_and_mobile(self):
        fixture=dict(base=self.live_server_url,path=reverse('crm_recurring',args=[self.deal.pk])+'?org_id='+str(self.org.pk),
            senderCookie=self.http.cookies[settings.SESSION_COOKIE_NAME].value,sessionCookieName=settings.SESSION_COOKIE_NAME)
        code,output=_bounded_browser_process(['node',str(Path(__file__).with_name('crm_recurring.browser.cjs'))],fixture)
        self.assertEqual(code,0,output)
        self.assertEqual(CrmRecurringDraft.objects.count(),1);self.assertEqual(CrmRecurringRevision.objects.count(),1)
        self.assertEqual(CrmRecurringConfirmation.objects.count(),1)
