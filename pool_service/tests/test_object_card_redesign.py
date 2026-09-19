from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import (
    Client,
    CrmItem,
    Organization,
    OrganizationAccess,
    Pool,
    ServiceVisitPlan,
    WaterReading,
)


class ObjectCardRoleRedesignTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Aqualine UI Test",
            plan_type=Organization.PLAN_COMPANY_PAID,
            paid_until=timezone.now() + timedelta(days=30),
        )
        self.client_record = Client.objects.create(
            name="Тестовый клиент",
            organization=self.organization,
        )
        self.pool = Pool.objects.create(
            client=self.client_record,
            organization=self.organization,
            address="Барнаул, тестовый объект",
            service_frequency=Pool.SERVICE_FREQ_WEEKLY,
            service_monthly_price="25000.00",
        )
        self.manager = User.objects.create_user(
            username="ui_manager",
            password="test-pass-123",
            first_name="Менеджер",
        )
        self.service = User.objects.create_user(
            username="ui_service",
            password="test-pass-123",
            first_name="Сервисник",
        )
        OrganizationAccess.objects.create(
            user=self.manager,
            organization=self.organization,
            role="manager",
        )
        OrganizationAccess.objects.create(
            user=self.service,
            organization=self.organization,
            role="service",
        )
        self.reading = WaterReading.objects.create(
            pool=self.pool,
            date=timezone.now() - timedelta(days=1),
            added_by=self.service,
            temperature=27.0,
            ph=7.2,
            cl_free=0.5,
            cl_total=0.7,
            comment="Вода прозрачная",
            required_materials="Хлор",
            performed_works="Промывка фильтра",
        )
        today = timezone.localdate()
        ServiceVisitPlan.objects.create(
            pool=self.pool,
            week_start=today,
            planned_date=today + timedelta(days=3),
            created_by=self.manager,
        )
        CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title="Проверить насос",
            client=self.client_record,
            pool=self.pool,
            stage=CrmItem.STAGE_SERVICE_NEW,
            created_by=self.manager,
        )

    def _get_detail(self, user):
        self.client.force_login(user)
        return self.client.get(reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))

    def test_manager_gets_manager_desktop_card_and_recent_visits(self):
        response = self._get_detail(self.manager)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["desktop_card_mode"], "manager")
        self.assertEqual(response.context["latest_reading"].id, self.reading.id)
        self.assertEqual(response.context["open_service_issue_count"], 1)
        self.assertContains(response, "Карточка менеджера")
        self.assertContains(response, "Короткая сводка для менеджера")
        self.assertContains(response, "Стоимость обслуживания")
        self.assertContains(response, "Промывка фильтра")
        self.assertContains(response, 'id="object-visits"', html=False)
        self.assertContains(response, "object-role-strip d-none d-lg-flex", html=False)
        self.assertNotContains(response, "Рабочая карточка сервисника")

    def test_service_gets_visit_first_desktop_card_without_manager_finance(self):
        response = self._get_detail(self.service)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["desktop_card_mode"], "service")
        self.assertEqual(response.context["open_service_issue_count"], 1)
        self.assertContains(response, "Рабочая карточка сервисника")
        self.assertContains(response, "Посещения и состояние объекта в приоритете")
        self.assertContains(response, "object-visit-history--primary", html=False)
        self.assertContains(response, "Новое посещение")
        self.assertContains(response, "Открытые задачи")
        self.assertNotContains(response, "Стоимость обслуживания")
        self.assertNotContains(response, "Короткая сводка для менеджера")
