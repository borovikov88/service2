"""Explicit local synthetic browser acceptance; never production credentials."""
import json
import os
import subprocess
from pathlib import Path
from unittest import skipUnless
from django.conf import settings
from django.test import override_settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from pool_service.tests.test_crm_role_views import RoleReviewUiTests
from pool_service.models import DataAuditLog
from pool_service.crm_role_models import CrmRolePolicy


@skipUnless(os.environ.get('CRM_ROLE_BROWSER_TEST') == '1', 'Explicit local browser gate')
@override_settings(CRM_ROLE_TRANSITION_ENABLED=True)
class RoleBrowserTests(StaticLiveServerTestCase):
    setUp = RoleReviewUiTests.setUp

    def test_preview_cancel_confirmation_repeat_and_mobile(self):
        payload = {'base': self.live_server_url, 'url': self.url,
            'cookieName': settings.SESSION_COOKIE_NAME,
            'cookie': self.client.cookies[settings.SESSION_COOKIE_NAME].value}
        result = subprocess.run(['node', str(Path(__file__).with_name('crm_role.browser.cjs'))],
            input=json.dumps(payload), text=True, capture_output=True, timeout=90)
        self.assertEqual(result.returncode, 0, (result.stdout + result.stderr)[-4000:])
        self.assertEqual(CrmRolePolicy.objects.get(user=self.target).roles, ['manager'])
        self.assertEqual(DataAuditLog.objects.filter(entity_type='crm_role_transition_v1').count(), 1)
