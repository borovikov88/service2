"""Synthetic quote UI acceptance; no external requests or sending."""
import os
from pathlib import Path
from unittest import skipUnless
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from pool_service.tests.test_crm_quotes import QuoteFixture
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process
from pool_service.crm_quote_models import CrmQuote, CrmQuoteEvent

@skipUnless(os.environ.get('CRM_LEADS_BROWSER_TEST')=='1','Dedicated local Chromium acceptance')
class QuoteBrowserTests(QuoteFixture,StaticLiveServerTestCase):
    def test_preview_cancel_accept_and_mobile_history(self):
        fixture=dict(base=self.live_server_url,path=self.url,version=self.version.pk,
            sessionCookie=self.http.cookies[settings.SESSION_COOKIE_NAME].value,
            sessionCookieName=settings.SESSION_COOKIE_NAME)
        code,output=_bounded_browser_process(['node',str(Path(__file__).with_name('crm_quote.browser.cjs'))],fixture)
        self.assertEqual(code,0,output)
        self.assertEqual(CrmQuote.objects.count(),1)
        self.assertEqual(CrmQuote.objects.get().status,'accepted')
        self.assertEqual(list(CrmQuoteEvent.objects.order_by('pk').values_list('action',flat=True)),['prepare','send','accept'])
