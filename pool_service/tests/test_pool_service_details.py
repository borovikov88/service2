from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from pool_service.models import Client, Organization, OrganizationAccess, Pool


class PoolServiceDetailsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Service org")
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
        )

    def test_accountant_sees_service_details_link_on_pool_detail(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("pool_detail", kwargs={"pool_uuid": self.pool.uuid}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Подробнее...")
        self.assertContains(response, reverse("pool_service_details", kwargs={"pool_uuid": self.pool.uuid}))

    def test_accountant_can_update_service_details(self):
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("pool_service_details", kwargs={"pool_uuid": self.pool.uuid}),
            {
                "service_monthly_price": "25000.50",
                "service_frequency": Pool.SERVICE_FREQ_WEEKLY,
                "service_details_comment": "Комментарий по обслуживанию",
            },
        )

        self.assertRedirects(response, reverse("pool_service_details", kwargs={"pool_uuid": self.pool.uuid}))
        self.pool.refresh_from_db()
        self.assertEqual(self.pool.service_monthly_price, Decimal("25000.50"))
        self.assertEqual(self.pool.service_frequency, Pool.SERVICE_FREQ_WEEKLY)
        self.assertEqual(self.pool.service_details_comment, "Комментарий по обслуживанию")
