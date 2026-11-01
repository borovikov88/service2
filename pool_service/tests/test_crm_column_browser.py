import json
import os
from pathlib import Path
from datetime import timedelta
from unittest import skipUnless
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.utils import timezone
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process
from pool_service.crm_commercial_models import CrmDeal, CrmLead
from pool_service.models import Organization


@skipUnless(os.environ.get('CRM_COLUMN_BROWSER_TEST')=='1','Local column-loading acceptance')
class ColumnBrowserTests(PilotFixture,StaticLiveServerTestCase):
    def test_load_expand_move_filters_table_mobile_and_interrupted_request(self):
        self.org.plan_type=Organization.PLAN_COMPANY_PAID
        self.org.paid_until=timezone.now()+timedelta(days=30)
        self.org.save(update_fields=['plan_type','paid_until'])
        source=self.pipeline.stages.get(code='need'); other=self.pipeline.stages.get(code='technical'); target=self.pipeline.stages.exclude(pk__in=[source.pk,other.pk]).order_by('position','pk').first()
        rows=[CrmDeal.objects.create(organization=self.org,pipeline=self.pipeline,stage=source,
            client=self.client_record,manager=self.user,title=f'Column synthetic {n}') for n in range(211)]
        for n in range(63):
            CrmDeal.objects.create(organization=self.org,pipeline=self.pipeline,stage=other,client=self.client_record,
                manager=self.user,title=f'Other column synthetic {n}')
        self.grant.view_leads=True;self.grant.transition_own_leads=True;self.grant.edit_own_leads=True;self.grant.save()
        lead_rows=[]
        for state,count in [('new',211),('working',63)]:
            for n in range(count):
                lead_rows.append(CrmLead.objects.create(organization=self.org,title=f'Synthetic lead {state} {n}',
                    need='Synthetic need',assignee=self.user,status=state))
        self.client.force_login(self.user)
        fixture=dict(base=self.live_server_url,org=self.org.pk,pipeline=self.pipeline.pk,source=source.pk,target=target.pk,other=other.pk,lead=lead_rows[10].pk,
            deal=rows[10].pk,sessionCookie=self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            sessionCookieName=settings.SESSION_COOKIE_NAME)
        code,output=_bounded_browser_process(['node',str(Path(__file__).with_name('crm_column_loading.browser.cjs'))],fixture)
        self.assertEqual(code,0,output)
        self.assertEqual(CrmDeal.objects.get(pk=rows[10].pk).stage_id,target.pk)
        self.assertEqual(CrmLead.objects.get(pk=lead_rows[10].pk).status,'closed')
