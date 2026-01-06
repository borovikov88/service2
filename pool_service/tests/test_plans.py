from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from pool_service.models import Client, Organization, Pool
from pool_service.services.permissions import (
    company_has_access,
    is_personal_free,
    trial_ends_at,
)


class PlanAccessTests(TestCase):
    def test_trial_end_is_14_days(self):
        start = timezone.now()
        org = Organization.objects.create(name="Test Org", trial_started_at=start)
        self.assertEqual(trial_ends_at(org), start + timedelta(days=14))

    def test_trial_expired_after_14_days(self):
        now = timezone.now()
        start = now - timedelta(days=14, seconds=1)
        org = Organization.objects.create(name="Test Org 2", trial_started_at=start)
        self.assertFalse(company_has_access(org, now=now))

    def test_personal_free_exact_one_pool(self):
        user = User.objects.create_user(username="9000000000", password="pass12345")
        client = Client.objects.create(
            user=user,
            client_type="private",
            first_name="Test",
            last_name="User",
            name="Test User",
        )
        Pool.objects.create(client=client, address="Addr 1")
        self.assertTrue(is_personal_free(user))
        Pool.objects.create(client=client, address="Addr 2")
        self.assertFalse(is_personal_free(user))
