"""Actual Chromium acceptance for failure drop targets, cancel and mobile access."""
from datetime import timedelta
import os
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_commercial_models import CrmDeal, CrmLead, CrmEvent
from pool_service.models import Organization, OrganizationAccess, ServiceTask
from pool_service.services.crm_commercial_commands import create_commercial_deal
from pool_service.tests.test_crm_lead_commands import LeadFixture
from pool_service.tests.test_crm_lead_kanban_browser import _bounded_browser_process


@skipUnless(os.environ.get('CRM_CARD_BROWSER_TEST') == '1', 'Explicit Chromium acceptance')
class FailureBrowserTests(LeadFixture, StaticLiveServerTestCase):
    def test_drop_cancel_confirmation_persistence_mobile_dark_and_readonly(self):
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.crm_close = True; self.org.save()
        OrganizationAccess.objects.filter(organization=self.org, user=self.user).update(role='owner')
        self.grant.view_team_deals = self.grant.transition_own_deals = self.grant.edit_own_deals = True
        # No conversion permission: the hidden successful drop target must stay hidden.
        self.grant.qualify_own_leads = False; self.grant.save()
        leads = [self.create(title='Synthetic failure lead '+str(n)) for n in range(2)]
        deals = []
        for n in range(2):
            result = create_commercial_deal(actor=self.user, organization=self.org, operation_key='failure-browser-'+str(n),
                client_id=self.client.pk, pipeline_id=self.pipeline.pk, title='Synthetic failure deal '+str(n),
                next_action={'title': 'Synthetic action', 'due_at': self.due})
            deals.append(CrmDeal.objects.get(pk=result['deal_id']))
        other = get_user_model().objects.create_user('synthetic-failure-other')
        OrganizationAccess.objects.create(organization=self.org, user=other, role='manager')
        readonly = CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline,
            stage=self.pipeline.stages.get(code='need'), manager=other, title='Synthetic readonly deal')
        old = CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline,
            stage=self.pipeline.stages.get(code='need'), manager=self.user, title='Synthetic previous loss',
            outcome='lost', close_reason='Synthetic previous refusal', closed_at=timezone.now())
        http = HttpClient(); http.force_login(self.user)
        fixture = dict(base=self.live_server_url, org=self.org.pk,
            leads=reverse('crm_live_leads')+'?org_id='+str(self.org.pk),
            deals=reverse('crm_live_workspace')+'?org_id='+str(self.org.pk)+'&pipeline='+str(self.pipeline.pk),
            leadIds=[row.pk for row in leads], dealIds=[row.pk for row in deals], readonlyId=readonly.pk,
            oldId=old.pk, failedStage=self.pipeline.stages.get(code='failed').pk,
            technicalStage=self.pipeline.stages.get(code='technical').pk,
            sessionCookie=http.cookies[settings.SESSION_COOKIE_NAME].value, sessionCookieName=settings.SESSION_COOKIE_NAME)
        with patch('pool_service.services.crm_assignment_notifications.send_push_to_users'), patch('pool_service.services.notifications.send_push_to_users'), patch('pool_service.services.push_notifications.send_push_to_users'):
            code, output = _bounded_browser_process(['node', str(Path(__file__).with_name('crm_failure.browser.cjs'))], fixture)
        self.assertEqual(code, 0, output)
        for lead in leads:
            lead.refresh_from_db(); self.assertEqual(lead.result, 'lost')
            self.assertEqual(CrmEvent.objects.filter(lead=lead, event_type='lead.disposition').count(), 1)
        for deal in deals:
            deal.refresh_from_db(); self.assertEqual((deal.outcome, deal.stage.code), ('lost', 'failed'))
            self.assertEqual(CrmEvent.objects.filter(deal=deal, event_type='deal.close_negative').count(), 1)
            self.assertEqual(deal.next_action.task_link.task.status, ServiceTask.STATUS_CANCELLED)
        old.refresh_from_db(); readonly.refresh_from_db()
        self.assertEqual((old.stage.code, old.revision, readonly.outcome), ('need', 0, 'open'))
