"""One real Chromium scenario; dedicated CI step explicitly enables this gate."""
import json
import os
from pathlib import Path
import subprocess
from unittest import skipUnless
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_commercial_models import CrmDeal
from pool_service.models import Pool, ServiceTask
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture


@skipUnless(os.environ.get('CRM_CARD_BROWSER_TEST') == '1', 'Dedicated Chromium acceptance step enables this test')
class CommercialCardBrowserTests(PilotFixture, StaticLiveServerTestCase):
    def test_today_filter_count_waiting_reload_and_mobile(self):
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=['paid_until'])
        now = timezone.now()
        for key, due in (('today', now), ('waiting', now), ('tomorrow', now + timedelta(days=1))):
            result = self.create(operation_key='browser-' + key, title='Synthetic ' + key,
                next_action={'title': 'Synthetic next ' + key, 'due_at': due})
            if key == 'waiting':
                task = ServiceTask.objects.get(pk=result['task_id'])
                task.status = ServiceTask.STATUS_WAITING
                task.end_date = task.due_at = None
                task.payload_json = {'task_feedback': {'mode': 'waiting', 'next_check_at': now.isoformat()}}
                task.save(update_fields=['status', 'end_date', 'due_at', 'payload_json'])
        self.client.force_login(self.user)
        fixture = {'base': self.live_server_url,
            'workspace': reverse('crm_live_workspace') + '?org_id=' + str(self.org.pk),
            'sessionCookie': self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            'sessionCookieName': settings.SESSION_COOKIE_NAME}
        # Hold the server's calendar day steady even across local midnight.
        with patch('pool_service.crm_live_views.timezone.now', return_value=now):
            result = subprocess.run(['node', str(Path(__file__).with_name('crm_today.browser.cjs'))],
                input=json.dumps(fixture), text=True, capture_output=True, timeout=90, check=False)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-6000:])

    def test_drawer_save_reload_object_drag_and_mobile_dark(self):
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=['paid_until'])
        pool = Pool.objects.create(organization=self.org, client=self.client_record, address='Browser existing synthetic object')
        self.client.force_login(self.user)
        payload = {
            'base': self.live_server_url,
            'workspace': reverse('crm_live_workspace') + '?org_id=' + str(self.org.pk),
            'data': reverse('crm_live_data') + '?org_id=' + str(self.org.pk),
            'detailPrefix': reverse('crm_live_detail', args=[1]).rsplit('1/', 1)[0],
            'orgId': self.org.pk, 'clientName': self.client_record.name,
            'sessionCookie': self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            'sessionCookieName': settings.SESSION_COOKIE_NAME,
            'poolId': pool.pk, 'stageId': self.pipeline.stages.get(code='technical').pk,
        }
        result = subprocess.run(['node', str(Path(__file__).with_name('crm_commercial_card.browser.cjs'))],
            input=json.dumps(payload), text=True, capture_output=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-6000:])
        deal = CrmDeal.objects.get(title='Browser synthetic card')
        self.assertEqual((deal.object_kind, deal.pool_id, deal.stage_id), ('pool', pool.pk, payload['stageId']))
        self.assertEqual(deal.work_subject, 'Browser synthetic installation')
        self.assertIsNone(deal.amount)
        self.assertEqual(deal.amount_basis, 'unknown')
        self.assertEqual(Pool.objects.count(), 1)
