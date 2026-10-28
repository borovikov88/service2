"""Dedicated actual Chromium acceptance; uses CI's pinned isolated dependencies."""
import json
import os
from pathlib import Path
import subprocess
from unittest import skipUnless

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse

from pool_service.crm_object_need_models import CrmObjectNeedReview
from pool_service.crm_object_need_models import CrmObjectNeedDealLine
from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import CrmDeal, CrmDealNextAction, CrmPipeline
from pool_service.models import ServiceTask
from pool_service.tests.test_crm_object_needs import ReviewFixture


@skipUnless(os.environ.get('CRM_NEEDS_BROWSER_TEST') == '1', 'Dedicated Chromium acceptance step enables this test')
class ObjectNeedBrowserTests(ReviewFixture, StaticLiveServerTestCase):
    def test_mixed_classification_lost_response_replay_search_reload_mobile(self):
        self.grant.create_own_deals = self.grant.manage_own_next_action = True
        self.grant.save()
        bootstrap_commercial_settings(self.org.pk)
        pipeline = CrmPipeline.objects.get(organization=self.org, code='retail')
        source_task = ServiceTask.objects.values().get(pk=self.task.pk)
        self.client.force_login(self.user)
        fixture = {'base': self.live_server_url,
            'workspace': reverse('crm_object_needs') + '?org_id=' + str(self.org.pk),
            'data': reverse('crm_object_need_data') + '?org_id=' + str(self.org.pk),
            'sessionCookie': self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            'sessionCookieName': settings.SESSION_COOKIE_NAME, 'taskId': self.task.pk,
            'pipelineId': pipeline.pk}
        result = subprocess.run(['node', str(Path(__file__).with_name('crm_object_need.browser.cjs'))],
            input=json.dumps(fixture), text=True, capture_output=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-8000:])
        self.assertEqual(CrmObjectNeedReview.objects.count(), 1)
        review = CrmObjectNeedReview.objects.get()
        self.assertEqual([row['mode'] for row in review.positions], ['chemistry_monthly','approval_required'])
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'new')
        self.assertEqual(ServiceTask.objects.values().get(pk=self.task.pk), source_task)
        self.assertEqual(CrmDeal.objects.count(), 1)
        self.assertEqual(CrmObjectNeedDealLine.objects.count(), 1)
        deal = CrmDeal.objects.get()
        self.assertEqual(deal.title, 'Synthetic agreed scope')
        self.assertEqual(deal.manager_id, self.user.pk)
        self.assertEqual(CrmDealNextAction.objects.get(deal=deal).task_link.task.task_type, 'crm_followup')
        self.assertEqual(ServiceTask.objects.filter(water_reading=self.reading).count(), 1)
