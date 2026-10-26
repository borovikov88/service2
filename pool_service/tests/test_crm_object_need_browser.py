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
from pool_service.tests.test_crm_object_needs import ReviewFixture


@skipUnless(os.environ.get('CRM_NEEDS_BROWSER_TEST') == '1', 'Dedicated Chromium acceptance step enables this test')
class ObjectNeedBrowserTests(ReviewFixture, StaticLiveServerTestCase):
    def test_mixed_classification_lost_response_replay_search_reload_mobile(self):
        self.client.force_login(self.user)
        fixture = {'base': self.live_server_url,
            'workspace': reverse('crm_object_needs') + '?org_id=' + str(self.org.pk),
            'data': reverse('crm_object_need_data') + '?org_id=' + str(self.org.pk),
            'sessionCookie': self.client.cookies[settings.SESSION_COOKIE_NAME].value,
            'sessionCookieName': settings.SESSION_COOKIE_NAME, 'taskId': self.task.pk}
        result = subprocess.run(['node', str(Path(__file__).with_name('crm_object_need.browser.cjs'))],
            input=json.dumps(fixture), text=True, capture_output=True, timeout=120, check=False)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-8000:])
        self.assertEqual(CrmObjectNeedReview.objects.count(), 1)
        review = CrmObjectNeedReview.objects.get()
        self.assertEqual([row['mode'] for row in review.positions], ['chemistry_monthly','approval_required'])
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, 'new')
