from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from pool_service.models import Client, Notification, Organization, OrganizationAccess, Pool, WaterReading
from pool_service.services.notifications import notify_reading_out_of_range


class LimitsNotificationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="Аквалайн",
            city="Москва",
            trial_started_at=timezone.now(),
            notify_limits=True,
            notify_limits_push=True,
        )
        self.org_admin = User.objects.create_user(username="orgadmin", password="pass")
        self.service_user = User.objects.create_user(username="serviceuser", password="pass")
        OrganizationAccess.objects.create(user=self.org_admin, organization=self.org, role="admin")
        OrganizationAccess.objects.create(user=self.service_user, organization=self.org, role="service")
        self.client = Client.objects.create(
            client_type="private",
            first_name="Тест",
            last_name="Клиент",
            name="Тест Клиент",
            phone="+79000000000",
            organization=self.org,
        )
        self.pool = Pool.objects.create(client=self.client, address="Тестовый бассейн", organization=self.org)

    def test_out_of_range_reading_from_org_employee_notifies_org_admin(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            ph=6.4,
        )

        created = notify_reading_out_of_range(reading)

        self.assertTrue(created)
        self.assertTrue(
            Notification.objects.filter(
                user=self.org_admin,
                kind="limits",
                pool=self.pool,
            ).exists()
        )
