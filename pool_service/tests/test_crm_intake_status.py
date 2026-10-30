from datetime import timedelta
from types import SimpleNamespace
from django.test import SimpleTestCase
from django.utils import timezone
from pool_service.services.crm_intake_status import intake_status


class IntakeStatusTests(SimpleTestCase):
    def setUp(self):
        self.now=timezone.now();self.org=SimpleNamespace(crm_intake_process=True)

    def test_no_attempt_is_not_success_or_empty_queue(self):
        result=intake_status(self.org,None,now=self.now)
        self.assertEqual(result['last_attempt_result'],'unknown')
        self.assertIsNone(result['last_successful_pass_at'])
        self.assertEqual(result['backlog'],{'status':'unavailable','count':None})

    def test_complete_pass_is_not_delivery_freshness(self):
        stamp=self.now-timedelta(hours=3)
        result=intake_status(self.org,SimpleNamespace(last_run_at=stamp,last_error=''),now=self.now)
        self.assertEqual(result['attempt_age_seconds'],10800)
        self.assertEqual(result['last_successful_pass_at'],stamp.isoformat())
        self.assertIsNone(result['backlog']['count'])

    def test_error_does_not_invent_previous_success_or_expose_code(self):
        result=intake_status(self.org,SimpleNamespace(last_run_at=self.now,last_error='private-details'),now=self.now)
        self.assertEqual(result['last_attempt_result'],'error')
        self.assertIsNone(result['last_successful_pass_at'])
        self.assertNotIn('private-details',str(result))

    def test_disabled_and_clock_skew_are_explicit(self):
        self.org.crm_intake_process=False
        result=intake_status(self.org,SimpleNamespace(last_run_at=self.now+timedelta(seconds=1),last_error=''),now=self.now)
        self.assertFalse(result['enabled']);self.assertEqual(result['attempt_age_seconds'],0)
