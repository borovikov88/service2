from django.test import TestCase, Client as HttpClient
from django.urls import reverse
from django.contrib.auth.models import User

from .models import Organization, OrganizationAccess, Client, Pool, PoolAccess


class PoolServiceFlowTests(TestCase):
    def setUp(self):
        self.http = HttpClient()
        # Базовая организация и админ
        self.org = Organization.objects.create(name="Аквалайн", city="Москва")
        self.org_admin = User.objects.create_user(username="orgadmin", password="pass", first_name="Иван", last_name="Иванов")
        OrganizationAccess.objects.create(user=self.org_admin, organization=self.org, role="admin")

        # Клиент с привязкой к организации
        self.client_user = User.objects.create_user(username="clientuser", password="pass", first_name="Петр", last_name="Петров")
        self.client_profile = Client.objects.create(
            user=self.client_user,
            client_type="private",
            first_name="Петр",
            last_name="Петров",
            name="Петр Петров",
            phone="+7 900 000 0000",
            organization=self.org,
        )

        # Вторая организация и ее клиент
        self.other_org = Organization.objects.create(name="Другая", city="СПб")
        other_user = User.objects.create_user(username="otheradmin", password="pass")
        OrganizationAccess.objects.create(user=other_user, organization=self.other_org, role="admin")
        self.other_client = Client.objects.create(
            client_type="private",
            first_name="Сергей",
            last_name="Другой",
            name="Сергей Другой",
            phone="+7 901 111 1111",
            organization=self.other_org,
        )

    def test_client_create_view_sets_organization(self):
        self.http.login(username="orgadmin", password="pass")
        url = reverse("client_create")
        payload = {
            "client_type": "private",
            "first_name": "Алексей",
            "last_name": "Новиков",
            "phone": "+7 902 333 2211",
            "email": "alex@example.com",
        }
        resp = self.http.post(url, payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        created = Client.objects.filter(first_name="Алексей", last_name="Новиков").last()
        self.assertIsNotNone(created, "Клиент не создался")
        self.assertEqual(created.organization, self.org, "Организация не проставлена у клиента")

    def test_pool_create_by_org_assigns_org_and_access(self):
        self.http.login(username="orgadmin", password="pass")
        url = reverse("pool_create")
        payload = {
            "client": str(self.client_profile.id),
            "address": "г. Москва, ул. Тестовая 1",
            "description": "",
        }
        resp = self.http.post(url, payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        pool = Pool.objects.filter(address__icontains="Тестовая").first()
        self.assertIsNotNone(pool, "Бассейн не создался")
        self.assertEqual(pool.client, self.client_profile)
        self.assertEqual(pool.organization, self.org, "Организация не проставлена у бассейна")
        self.assertTrue(PoolAccess.objects.filter(user=self.org_admin, pool=pool).exists(), "Создателю не выдан доступ")
        self.assertTrue(PoolAccess.objects.filter(user=self.client_user, pool=pool).exists(), "Клиенту не выдан доступ")

    def test_pool_create_by_client_uses_self_client(self):
        # самостоятельный клиент без организации
        solo_user = User.objects.create_user(username="soloclient", password="pass", first_name="Соло", last_name="Клиент")
        solo_client = Client.objects.create(
            user=solo_user,
            client_type="private",
            first_name="Соло",
            last_name="Клиент",
            name="Соло Клиент",
            phone="+7 905 111 2233",
            organization=None,
        )

        self.http.login(username="soloclient", password="pass")
        url = reverse("pool_create")
        payload = {
            "client": str(solo_client.id),
            "address": "Адрес клиента",
            "description": "",
        }
        resp = self.http.post(url, payload, follow=True)
        self.assertEqual(resp.status_code, 200)
        pool = Pool.objects.filter(address="Адрес клиента").first()
        self.assertIsNotNone(pool)
        self.assertEqual(pool.client, solo_client)
        self.assertIsNone(pool.organization, "Для самостоятельного клиента организация должна быть пустой")
        self.assertTrue(PoolAccess.objects.filter(user=solo_user, pool=pool).exists())

    def test_pool_list_filtered_by_organization(self):
        # Пул для другой организации не должен быть виден текущему администратору
        foreign_pool = Pool.objects.create(client=self.other_client, address="Чужой адрес", organization=self.other_org)
        own_pool = Pool.objects.create(client=self.client_profile, address="Свой адрес", organization=self.org)

        self.http.login(username="orgadmin", password="pass")
        resp = self.http.get(reverse("pool_list"))
        self.assertEqual(resp.status_code, 200)
        pools = list(resp.context["pools"])
        addresses = {p.address for p in pools}
        self.assertIn("Свой адрес", addresses)
        self.assertNotIn("Чужой адрес", addresses)
