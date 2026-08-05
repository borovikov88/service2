from decimal import Decimal
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, Organization, OrganizationAccess, Pool


class PoolServiceDetailsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Service org", paid_until=timezone.now() + timedelta(days=30))
        self.user = User.objects.create_user(username="accountant", password="password")
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.organization,
            role="accountant",
        )
        self.client_obj = Client.objects.create(
            name="Клиент",
            organization=self.organization,
        )
        self.pool = Pool.objects.create(
            client=self.client_obj,
            organization=self.organization,
            address="Адрес объекта",
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
                "object_type": Pool.OBJECT_TYPE_POOL,
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
