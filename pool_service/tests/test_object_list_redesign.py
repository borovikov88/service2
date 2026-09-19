from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, Organization, OrganizationAccess, Pool, WaterReading


class ObjectListDesktopRedesignTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Aqualine Object List UI",
            plan_type=Organization.PLAN_COMPANY_PAID,
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.user = User.objects.create_user(
            username="object_list_manager",
            password="test-pass-123",
        )
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.organization,
            role="manager",
        )
        self.client_record = Client.objects.create(
            name="Клиент списка",
            organization=self.organization,
        )
        self.pool = Pool.objects.create(
            client=self.client_record,
            organization=self.organization,
            address="Барнаул, ул. Тестовая, 10",
            service_frequency=Pool.SERVICE_FREQ_WEEKLY,
        )
        self.reading = WaterReading.objects.create(
            pool=self.pool,
            date=timezone.now() - timedelta(days=2),
            added_by=self.user,
            ph=7.2,
        )

    def test_desktop_object_card_uses_existing_service_data(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("pool_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "На обслуживании")
        self.assertContains(response, "Последнее посещение")
        self.assertContains(response, "Частота")
        self.assertNotContains(response, "Следующий выезд")
        self.assertNotContains(response, "История</div>", html=False)
        self.assertNotContains(response, "next_visit_date", html=False)
        self.assertContains(response, "pool-object-card__desktop d-none d-lg-block", html=False)
        self.assertContains(response, "pool-icon-svg", html=False)

    def test_desktop_object_card_marks_suspended_service(self):
        self.pool.service_status = Pool.SERVICE_STATUS_SUSPENDED
        self.pool.service_suspended = True
        self.pool.save(update_fields=["service_status", "service_suspended"])

        self.client.force_login(self.user)
        response = self.client.get(reverse("pool_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Обслуживание приостановлено")
        self.assertContains(response, "pool-object-status--suspended", html=False)

    def test_desktop_object_card_shows_winterized_and_stopped_statuses(self):
        for status, label in (
            (Pool.SERVICE_STATUS_WINTERIZED, "Законсервирован"),
            (Pool.SERVICE_STATUS_STOPPED, "Больше не обслуживаем"),
        ):
            self.pool.service_status = status
            self.pool.service_suspended = True
            self.pool.save(update_fields=["service_status", "service_suspended"])

            self.client.force_login(self.user)
            response = self.client.get(reverse("pool_list"))

            self.assertEqual(response.status_code, 200)
            self.assertContains(response, label)
            self.assertContains(response, f"pool-object-status--{status}", html=False)
