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

    def test_desktop_object_card_is_simplified_and_uses_pool_icon(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("pool_list"))

        self.assertEqual(response.status_code, 200)
        listed_pool = list(response.context["pools"])[0]
        self.assertIsNotNone(listed_pool.last_reading_display)
        self.assertContains(response, "На обслуживании")
        self.assertContains(response, "Последнее посещение")
        self.assertContains(response, "Частота")
        self.assertContains(response, "pool-object-card__pool-icon", html=False)
        self.assertContains(response, "pool-object-card__desktop d-none d-lg-block", html=False)
        self.assertNotContains(response, "Следующий выезд")
        self.assertNotContains(response, ">История</div>", html=False)
        self.assertNotContains(response, "next_visit_date", html=False)

    def test_desktop_object_card_renders_all_service_statuses(self):
        self.client.force_login(self.user)
        cases = (
            (Pool.SERVICE_STATUS_ACTIVE, "На обслуживании", "pool-object-status--active"),
            (Pool.SERVICE_STATUS_PAUSED, "Обслуживание приостановлено", "pool-object-status--paused"),
            (Pool.SERVICE_STATUS_CONSERVED, "Законсервирован", "pool-object-status--conserved"),
            (Pool.SERVICE_STATUS_ENDED, "Больше не обслуживаем", "pool-object-status--ended"),
        )

        for status, label, css_class in cases:
            with self.subTest(status=status):
                self.pool.service_status = status
                self.pool.save(update_fields=["service_status"])
                response = self.client.get(reverse("pool_list"))
                self.assertContains(response, label)
                self.assertContains(response, css_class, html=False)
