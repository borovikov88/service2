from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, Organization, OrganizationAccess, Pool, ServiceVisitPlan, WaterReading


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
        self.next_visit = timezone.localdate() + timedelta(days=4)
        ServiceVisitPlan.objects.create(
            pool=self.pool,
            week_start=timezone.localdate(),
            planned_date=self.next_visit,
            created_by=self.user,
        )

    def test_desktop_object_card_uses_existing_service_data(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("pool_list"))

        self.assertEqual(response.status_code, 200)
        listed_pool = list(response.context["pools"])[0]
        self.assertEqual(listed_pool.next_visit_date, self.next_visit)
        self.assertContains(response, "На обслуживании")
        self.assertContains(response, "Последнее посещение")
        self.assertContains(response, "Следующий выезд")
        self.assertContains(response, "Частота")
        self.assertContains(response, "История")
        self.assertContains(response, "1 записей")
        self.assertContains(response, self.next_visit.strftime("%d.%m.%Y"))
        self.assertContains(response, "pool-object-card__desktop d-none d-lg-block", html=False)

    def test_desktop_object_card_marks_suspended_service(self):
        self.pool.service_suspended = True
        self.pool.save(update_fields=["service_suspended"])

        self.client.force_login(self.user)
        response = self.client.get(reverse("pool_list"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Обслуживание приостановлено")
        self.assertContains(response, "pool-object-status--paused", html=False)
