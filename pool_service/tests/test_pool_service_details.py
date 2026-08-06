from decimal import Decimal
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, DataAuditLog, Organization, OrganizationAccess, Pool, WaterReading


class PoolServiceDetailsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Service org", paid_until=timezone.now() + timedelta(days=30))
        self.user = User.objects.create_user(username="accountant", password="password")
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.organization,
            role="accountant",
        )
        self.admin_user = User.objects.create_user(username="admin", password="password")
        OrganizationAccess.objects.create(
            user=self.admin_user,
            organization=self.organization,
            role="admin",
        )
        self.client_obj = Client.objects.create(
            name="Клиент",
            organization=self.organization,
        )
        self.pool = Pool.objects.create(
            client=self.client_obj,
            organization=self.organization,
            address="Адрес объекта",
            object_type=Pool.OBJECT_TYPE_POOL,
            shape="rect",
            pool_type="skimmer",
            length=6.75,
            width=3.73,
            depth_min=1.5,
            depth_max=2.0,
            variable_depth=True,
            surface_area=25.18,
            volume=44.06,
            service_frequency=Pool.SERVICE_FREQ_MONTHLY,
            service_monthly_price=Decimal("15000.00"),
            service_details_comment="Внутренний комментарий",
        )
        self.service_user = User.objects.create_user(username="service", password="password")
        OrganizationAccess.objects.create(
            user=self.service_user,
            organization=self.organization,
            role="service",
        )

    def test_accountant_sees_service_details_on_pool_detail_without_extra_page_link(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Стоимость в месяц")
        self.assertContains(response, "15\xa0000,00")
        self.assertContains(response, "Внутренний комментарий")
        self.assertNotContains(response, "Подробнее...")
        self.assertNotContains(response, reverse("pool_service_details", kwargs={"pool_uuid": self.pool.uuid}))

    def test_service_user_does_not_see_financial_service_details(self):
        self.client.force_login(self.service_user)

        response = self.client.get(reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Частота визитов")
        self.assertNotContains(response, "Стоимость в месяц")
        self.assertNotContains(response, "Внутренний комментарий")

    def test_accountant_can_update_service_details_from_pool_edit(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("pool_edit", kwargs={"pool_uuid": self.pool.uuid}),
            {
                "client": self.client_obj.id,
                "address": self.pool.address,
                "description": "",
                "object_type": Pool.OBJECT_TYPE_WATER,
                "shape": "rect",
                "pool_type": "skimmer",
                "length": "",
                "width": "",
                "diameter": "",
                "depth": "",
                "depth_min": "",
                "depth_max": "",
                "overflow_volume": "",
                "surface_area": "",
                "volume": "",
                "service_monthly_price": "25000.50",
                "service_frequency": Pool.SERVICE_FREQ_WEEKLY,
                "service_details_comment": "Комментарий по обслуживанию",
            },
        )

        self.assertRedirects(response, reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.service_monthly_price, Decimal("25000.50"))
        self.assertEqual(self.pool.service_frequency, Pool.SERVICE_FREQ_WEEKLY)
        self.assertEqual(self.pool.service_details_comment, "Комментарий по обслуживанию")
        self.assertEqual(self.pool.object_type, Pool.OBJECT_TYPE_POOL)
        self.assertEqual(self.pool.length, 6.75)
        self.assertEqual(self.pool.width, 3.73)
        self.assertEqual(self.pool.surface_area, 25.18)
        self.assertEqual(self.pool.volume, 44.06)

    def test_pool_type_change_with_history_requires_explicit_confirmation(self):
        WaterReading.objects.create(
            pool=self.pool,
            date=timezone.now(),
            added_by=self.admin_user,
            comment="История есть",
        )
        self.client.force_login(self.admin_user)

        payload = {
            "client": self.client_obj.id,
            "address": self.pool.address,
            "description": "",
            "object_type": Pool.OBJECT_TYPE_WATER,
            "shape": "rect",
            "pool_type": "skimmer",
            "length": "6.75",
            "width": "3.73",
            "diameter": "",
            "depth": "",
            "depth_min": "1.5",
            "depth_max": "2.0",
            "overflow_volume": "",
            "surface_area": "25.18",
            "volume": "44.06",
            "service_frequency": Pool.SERVICE_FREQ_MONTHLY,
            "service_monthly_price": "15000.00",
            "service_details_comment": "Внутренний комментарий",
        }
        response = self.client.post(reverse("pool_edit", kwargs={"pool_uuid": self.pool.uuid}), payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Подтвердите смену типа объекта")
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.object_type, Pool.OBJECT_TYPE_POOL)

        payload["confirm_object_type_change"] = "on"
        response = self.client.post(reverse("pool_edit", kwargs={"pool_uuid": self.pool.uuid}), payload)
        self.assertRedirects(response, reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.object_type, Pool.OBJECT_TYPE_WATER)
        audit = DataAuditLog.objects.filter(
            entity_type="pool",
            entity_id=str(self.pool.uuid),
            action=DataAuditLog.ACTION_UPDATE,
        ).latest("created_at")
        self.assertIn("object_type", audit.changed_fields)

    def test_water_reading_create_writes_audit_log(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(
            reverse("water_reading_create", kwargs={"pool_uuid": self.pool.uuid}),
            {
                "date": timezone.now().strftime("%Y-%m-%d %H:%M:%S"),
                "comment": "Проверка аудита",
            },
        )

        self.assertRedirects(response, reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))
        reading = WaterReading.objects.get(comment="Проверка аудита")
        audit = DataAuditLog.objects.get(entity_type="water_reading", entity_id=str(reading.uuid))
        self.assertEqual(audit.action, DataAuditLog.ACTION_CREATE)
        self.assertEqual(audit.actor, self.admin_user)
        self.assertEqual(audit.pool, self.pool)

    def test_water_reading_soft_delete_hides_and_restore_returns_it(self):
        reading = WaterReading.objects.create(
            pool=self.pool,
            date=timezone.now(),
            added_by=self.service_user,
            comment="soft delete reading",
        )
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse("water_reading_delete", kwargs={"reading_uuid": reading.uuid}))

        self.assertRedirects(response, reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))
        reading.refresh_from_db()
        self.assertTrue(reading.is_deleted)
        self.assertEqual(reading.deleted_by, self.admin_user)
        self.assertTrue(
            DataAuditLog.objects.filter(
                entity_type="water_reading",
                entity_id=str(reading.uuid),
                action=DataAuditLog.ACTION_DELETE,
            ).exists()
        )
        detail = self.client.get(reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))
        self.assertNotContains(detail, "soft delete reading")

        response = self.client.post(reverse("water_reading_restore", kwargs={"reading_uuid": reading.uuid}))

        self.assertRedirects(response, reverse("archive_list"))
        reading.refresh_from_db()
        self.assertFalse(reading.is_deleted)
        self.assertIsNone(reading.deleted_by)
        detail = self.client.get(reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))
        self.assertContains(detail, "soft delete reading")

    def test_pool_soft_delete_hides_object_and_writes_audit_log(self):
        self.client.force_login(self.admin_user)

        response = self.client.post(reverse("pool_delete", kwargs={"pool_uuid": self.pool.uuid}))

        self.assertRedirects(response, reverse("pool_list"))
        self.pool.refresh_from_db()
        self.assertTrue(self.pool.is_deleted)
        self.assertEqual(self.pool.deleted_by, self.admin_user)
        self.assertTrue(
            DataAuditLog.objects.filter(
                entity_type="pool",
                entity_id=str(self.pool.uuid),
                action=DataAuditLog.ACTION_DELETE,
            ).exists()
        )
        detail = self.client.get(reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))
        self.assertEqual(detail.status_code, 404)
        listing = self.client.get(reverse("pool_list"))
        self.assertNotContains(listing, self.pool.address)
