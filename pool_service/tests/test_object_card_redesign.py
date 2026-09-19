from datetime import datetime, timedelta, timezone as datetime_timezone
import importlib
from zoneinfo import ZoneInfo

from django.apps import apps

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
    PoolStatusHistory,
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

    def test_manager_gets_compact_manager_desktop_card(self):
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
        self.assertContains(response, "grid-template-columns: 320px minmax(0, 1fr)", html=False)
        self.assertEqual(response.context["audit_logs"], [])
        self.assertNotContains(response, "Журнал изменений")
        self.assertNotContains(response, 'href="#object-changes"', html=False)
        self.assertContains(response, 'data-bs-target="#serviceIssueCreateModal"', html=False)
        self.assertContains(response, 'id="serviceIssueCreateModal"', html=False)
        self.assertContains(response, 'id="service-issue-form"', html=False)
        self.assertNotContains(response, "Последние посещения")
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
        self.assertNotContains(response, "Короткая сводка для менеджера")

    def test_status_change_is_saved_and_rendered_in_visit_timeline(self):
        self.client.force_login(self.manager)
        response = self.client.post(
            reverse("pool_edit", kwargs={"pool_uuid": self.pool.uuid}),
            {
                "service_frequency": Pool.SERVICE_FREQ_WEEKLY,
                "service_monthly_price": "25000.00",
                "service_details_comment": "",
                "service_status": Pool.SERVICE_STATUS_WINTERIZED,
            },
        )

        self.assertEqual(response.status_code, 302)
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.service_status, Pool.SERVICE_STATUS_WINTERIZED)
        self.assertTrue(self.pool.service_suspended)

        change = PoolStatusHistory.objects.get(pool=self.pool)
        self.assertEqual(change.changed_by, self.manager)
        self.assertEqual(change.old_status, Pool.SERVICE_STATUS_ACTIVE)
        self.assertEqual(change.new_status, Pool.SERVICE_STATUS_WINTERIZED)
        self.assertIn("Законсервирован", change.comment)

        detail = self._get_detail(self.manager)
        self.assertContains(detail, "Статус изменён:")
        self.assertContains(detail, "Законсервирован")
        self.assertContains(detail, "object-status-timeline-row", html=False)

    def test_visit_time_is_saved_in_active_user_timezone(self):
        profile, _ = Profile.objects.get_or_create(user=self.service)
        profile.timezone = "Asia/Barnaul"
        profile.save(update_fields=["timezone"])

        self.client.force_login(self.service)
        response = self.client.post(
            reverse("water_reading_create", kwargs={"pool_uuid": self.pool.uuid}),
            {
                "date": "2026-09-19T16:00:00",
                "comment": "Проверка времени",
            },
        )

        self.assertEqual(response.status_code, 302)
        reading = WaterReading.objects.filter(
            pool=self.pool,
            comment="Проверка времени",
        ).latest("id")
        self.assertTrue(timezone.is_aware(reading.date))
        local_value = timezone.localtime(reading.date, ZoneInfo("Asia/Barnaul"))
        self.assertEqual(local_value.strftime("%d.%m.%Y %H:%M"), "19.09.2026 16:00")

        detail = self.client.get(reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))
        self.assertContains(detail, "19.09.2026 16:00")

    def test_legacy_visit_time_normalization_uses_author_timezone(self):
        profile, _ = Profile.objects.get_or_create(user=self.service)
        profile.timezone = "Asia/Barnaul"
        profile.save(update_fields=["timezone"])

        legacy_reading = WaterReading.objects.create(
            pool=self.pool,
            date=datetime(2026, 6, 12, 14, 0, tzinfo=datetime_timezone.utc),
            added_by=self.service,
            comment="Старое локальное время",
        )

        migration = importlib.import_module(
            "pool_service.migrations.0105_normalize_water_reading_timezone"
        )
        migration.normalize_legacy_water_reading_dates(apps, None)

        legacy_reading.refresh_from_db()
        self.assertEqual(
            legacy_reading.date,
            datetime(2026, 6, 12, 7, 0, tzinfo=datetime_timezone.utc),
        )
        self.assertEqual(
            timezone.localtime(
                legacy_reading.date,
                ZoneInfo("Asia/Barnaul"),
            ).strftime("%d.%m.%Y %H:%M"),
            "12.06.2026 14:00",
        )
