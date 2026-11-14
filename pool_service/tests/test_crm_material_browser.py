import os
from pathlib import Path
from unittest import skipUnless
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from pool_service.tests.test_crm_materials import MaterialFixture
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process
from pool_service.crm_material_models import CrmMaterialIssue,CrmMaterialMovement
from pool_service.services.crm_materials import balances,today


@skipUnless(os.environ.get('CRM_MATERIAL_BROWSER_TEST')=='1','Dedicated local material browser scenario')
class MaterialBrowserTests(MaterialFixture,StaticLiveServerTestCase):
    def test_issue_cancel_repeat_partial_delivery_return_and_responsive_history(self):
        self.client.force_login(self.user)
        fixture=dict(base=self.live_server_url,org=self.org.pk,pool=self.pool.pk,other=self.other_pool.pk,
            today=today().isoformat(),issued=self.issue_day.isoformat(),sessionCookie=self.client.cookies[settings.SESSION_COOKIE_NAME].value,sessionCookieName=settings.SESSION_COOKIE_NAME)
        code,output=_bounded_browser_process(['node',str(Path(__file__).with_name('crm_materials.browser.cjs'))],fixture,timeout=150)
        self.assertEqual(code,0,output)
        row=CrmMaterialIssue.objects.get();self.assertEqual(row.movements.count(),2)
        totals=balances(row);self.assertEqual((totals['delivered'],totals['returned'],totals['held']),(2,1,0))
        self.assertEqual(CrmMaterialMovement.objects.get(kind='delivery').pool_id,self.other_pool.pk)
