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
    PoolServiceStatusEvent,
    Profile,
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

    def _expected_open_service_issue_count(self):
        return (
            CrmItem.objects.filter(
                organization=self.organization,
                pool=self.pool,
                direction=CrmItem.DIRECTION_SERVICE,
            )
            .exclude(stage=CrmItem.STAGE_SERVICE_DONE)
            .count()
        )

    def test_manager_gets_manager_desktop_card(self):
        response = self._get_detail(self.manager)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["desktop_card_mode"], "manager")
        self.assertEqual(response.context["latest_reading"].id, self.reading.id)
        self.assertEqual(
            response.context["open_service_issue_count"],
            self._expected_open_service_issue_count(),
        )
        self.assertContains(response, "Карточка менеджера")
        self.assertContains(response, "Стоимость обслуживания")
        self.assertContains(response, "Промывка фильтра")
        self.assertContains(response, 'id="object-visits"', html=False)
        self.assertContains(response, "object-role-strip d-none d-lg-flex", html=False)
        self.assertContains(response, 'class="object-layout-wide"', html=False)
        self.assertContains(response, 'class="object-layout-wide__aside"', html=False)
        self.assertContains(response, 'class="object-info-grid row g-2 mt-3"', html=False)
        self.assertContains(response, "grid-template-columns: 330px minmax(0, 1fr)", html=False)
        self.assertContains(response, ".object-layout-wide__aside .object-info-grid > .col-lg-4", html=False)
        self.assertEqual(response.context["audit_logs"], [])
        self.assertNotContains(response, "Журнал изменений")
        self.assertNotContains(response, 'href="#object-changes"', html=False)
        self.assertContains(response, 'data-bs-target="#serviceIssueCreateModal"', html=False)
        self.assertContains(response, 'id="serviceIssueCreateModal"', html=False)
        self.assertContains(response, 'id="service-issue-form"', html=False)
        self.assertNotContains(response, "Рабочая карточка сервисника")

    def test_service_gets_visit_first_desktop_card_without_manager_finance(self):
        response = self._get_detail(self.service)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["desktop_card_mode"], "service")
        self.assertEqual(
            response.context["open_service_issue_count"],
            self._expected_open_service_issue_count(),
        )
        self.assertContains(response, "Рабочая карточка сервисника")
        self.assertContains(response, "Посещения и состояние объекта в приоритете")
        self.assertContains(response, "object-visit-history--primary", html=False)
        self.assertContains(response, "Новое посещение")
        self.assertContains(response, "Открытые задачи")
        self.assertNotContains(response, "Журнал изменений")
        self.assertNotContains(response, 'href="#object-changes"', html=False)
        self.assertContains(response, 'data-bs-target="#serviceIssueCreateModal"', html=False)
        self.assertNotContains(response, "Стоимость обслуживания")

    def test_manager_status_change_creates_history_event_and_syncs_legacy_flag(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("pool_edit", kwargs={"pool_uuid": self.pool.uuid}),
            {
                "service_frequency": Pool.SERVICE_FREQ_WEEKLY,
                "service_monthly_price": "25000.00",
                "service_details_comment": "Тест",
                "service_status": Pool.SERVICE_STATUS_WINTERIZED,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.service_status, Pool.SERVICE_STATUS_WINTERIZED)
        self.assertTrue(self.pool.service_suspended)

        event = PoolServiceStatusEvent.objects.get(pool=self.pool)
        self.assertEqual(event.previous_status, Pool.SERVICE_STATUS_ACTIVE)
        self.assertEqual(event.status, Pool.SERVICE_STATUS_WINTERIZED)
        self.assertEqual(event.changed_by, self.manager)

        detail = self._get_detail(self.manager)
        self.assertContains(detail, "Законсервирован")
        self.assertContains(detail, "Статус изменён: На обслуживании → Законсервирован")
        self.assertContains(detail, "Менеджер")

    def test_new_visit_keeps_browser_local_time_as_correct_aware_instant(self):
        profile, _ = Profile.objects.get_or_create(user=self.service)
        profile.timezone = "Asia/Barnaul"
        profile.save(update_fields=["timezone"])

        self.client.force_login(self.service)
        response = self.client.post(
            reverse("water_reading_create", kwargs={"pool_uuid": self.pool.uuid}),
            {
                "date": "2026-09-19T16:30:00",
                "temperature": "28",
            },
        )
        self.assertEqual(response.status_code, 302)

        created = WaterReading.objects.filter(pool=self.pool, temperature=28).latest("id")
        self.assertTrue(timezone.is_aware(created.date))
        self.assertEqual(created.date.astimezone(timezone.get_fixed_timezone(420)).strftime("%H:%M"), "16:30")

        detail = self._get_detail(self.service)
        self.assertContains(detail, "19.09.2026 16:30")
