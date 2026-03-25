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
            notify_limits_pool_staff=True,
            notify_limits_pool_staff_push=True,
            notify_limits_service_staff=True,
            notify_limits_service_staff_push=True,
        )
        self.org_admin = User.objects.create_user(username="orgadmin", password="pass")
        self.service_user = User.objects.create_user(username="serviceuser", password="pass")
        self.pool_staff_user = User.objects.create_user(username="poolstaff", password="pass")
        OrganizationAccess.objects.create(user=self.org_admin, organization=self.org, role="admin")
        OrganizationAccess.objects.create(user=self.service_user, organization=self.org, role="service")
        self.client = Client.objects.create(
            user=self.pool_staff_user,
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

        notify_reading_out_of_range(reading)

        self.assertTrue(Notification.objects.filter(user=self.org_admin, kind="limits", pool=self.pool).exists())

    def test_out_of_range_reading_create_signal_notifies_org_admin(self):
        WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            ph=6.4,
        )

        self.assertTrue(Notification.objects.filter(user=self.org_admin, kind="limits", pool=self.pool).exists())

    def test_out_of_range_reading_from_pool_staff_notifies_org_admin(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.pool_staff_user,
            date=timezone.now(),
            ph=6.4,
        )

        notify_reading_out_of_range(reading)

        self.assertTrue(Notification.objects.filter(user=self.org_admin, kind="limits", pool=self.pool).exists())

    def test_service_staff_limits_can_be_disabled_separately(self):
        self.org.notify_limits_service_staff = False
        self.org.notify_limits_service_staff_push = False
        self.org.save(update_fields=["notify_limits_service_staff", "notify_limits_service_staff_push"])
        reading = WaterReading.objects.create(
            pool=self.pool,
            added_by=self.service_user,
            date=timezone.now(),
            ph=6.4,
        )

        created = notify_reading_out_of_range(reading)

        self.assertFalse(created)
        self.assertFalse(Notification.objects.filter(kind="limits", pool=self.pool).exists())
