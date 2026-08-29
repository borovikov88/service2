from django.test import TestCase, Client as HttpClient
from django.urls import reverse
from django.contrib.auth.models import User
from django.utils import timezone

from ..models import Organization, OrganizationAccess, Client, Pool, PoolAccess, WaterReading, Notification
from ..services.notifications import notify_reading_out_of_range


class PoolServiceFlowTests(TestCase):
    def setUp(self):
        self.http = HttpClient()
        # Базовая организация и админ
        self.org = Organization.objects.create(name="Аквалайн", city="Москва", trial_started_at=timezone.now())
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
        self.other_org = Organization.objects.create(name="Другая", city="СПб", trial_started_at=timezone.now())
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

    def client_create_payload(self, *, first_name="Алексей", last_name="Новиков"):
        return {
            "client_type": "private",
            "first_name": first_name,
            "last_name": last_name,
            "phone": "+7 902 333 2211",
            "email": "alex@example.com",
        }

    def test_client_list_create_uses_clients_list_return_context(self):
        self.http.login(username="orgadmin", password="pass")
        client_create_url = reverse("client_create")
        clients_list_url = reverse("clients_list")
        pool_list_url = reverse("pool_list")

        listing = self.http.get(clients_list_url)
        self.assertEqual(
            listing.context["page_action_url"],
            f"{client_create_url}?return_to=clients_list",
        )

        form = self.http.get(client_create_url, {"return_to": "clients_list"})
        self.assertEqual(form.context["return_url"], clients_list_url)
        self.assertContains(form, 'name="return_to" value="clients_list"')
        self.assertContains(form, f'href="{clients_list_url}"')

        saved = self.http.post(
            client_create_url,
            {**self.client_create_payload(), "return_to": "clients_list"},
        )
        self.assertRedirects(saved, pool_list_url, fetch_redirect_response=False)

    def test_pool_create_client_link_and_return_context(self):
        self.http.login(username="orgadmin", password="pass")
        client_create_url = reverse("client_create")
        pool_create_url = reverse("pool_create")

        pool_form = self.http.get(pool_create_url)
        self.assertContains(pool_form, f'href="{client_create_url}?return_to=pool_create"')

        form = self.http.get(client_create_url, {"return_to": "pool_create"})
        self.assertEqual(form.context["return_url"], pool_create_url)
        self.assertContains(form, 'name="return_to" value="pool_create"')
        self.assertContains(form, f'href="{pool_create_url}"')

        invalid = self.http.post(
            client_create_url,
            {"client_type": "private", "first_name": "", "return_to": "pool_create"},
        )
        self.assertEqual(invalid.status_code, 200)
        self.assertContains(invalid, 'name="return_to" value="pool_create"')
        self.assertContains(invalid, f'href="{pool_create_url}"')

        saved = self.http.post(
            client_create_url,
            {**self.client_create_payload(first_name="Павел", last_name="Сидоров"), "return_to": "pool_create"},
        )
        created = Client.objects.get(first_name="Павел", last_name="Сидоров")
        self.assertRedirects(
            saved,
            f"{pool_create_url}?client_id={created.id}",
            fetch_redirect_response=False,
        )

    def test_client_create_discards_legacy_and_forged_navigation_context(self):
        self.http.login(username="orgadmin", password="pass")
        client_create_url = reverse("client_create")
        clients_list_url = reverse("clients_list")
        pool_list_url = reverse("pool_list")

        for query in (
            {"next": "/pools/create/"},
            {"return_to": "https://example.invalid/"},
        ):
            response = self.http.get(client_create_url, query)
            self.assertEqual(response.context["return_url"], clients_list_url)
            self.assertNotContains(response, "example.invalid")
            self.assertNotContains(response, 'name="next"')
            self.assertNotContains(response, 'name="return_to"')
            self.assertContains(response, f'href="{clients_list_url}"')

        invalid_forged = self.http.post(
            client_create_url,
            {"client_type": "private", "first_name": "", "return_to": "https://example.invalid/"},
        )
        self.assertEqual(invalid_forged.status_code, 200)
        self.assertNotContains(invalid_forged, "example.invalid")
        self.assertNotContains(invalid_forged, 'name="return_to"')
        self.assertContains(invalid_forged, f'href="{clients_list_url}"')

        legacy = self.http.post(
            client_create_url,
            {**self.client_create_payload(first_name="Олег", last_name="Петров"), "next": "/pools/create/"},
        )
        self.assertRedirects(legacy, pool_list_url, fetch_redirect_response=False)

        forged = self.http.post(
            client_create_url,
            {**self.client_create_payload(first_name="Максим", last_name="Котов"), "return_to": "https://example.invalid/"},
        )
        self.assertRedirects(forged, pool_list_url, fetch_redirect_response=False)

        direct = self.http.post(
            client_create_url,
            self.client_create_payload(first_name="Роман", last_name="Иванов"),
        )
        self.assertRedirects(direct, pool_list_url, fetch_redirect_response=False)

    def test_client_create_keeps_existing_access_guard(self):
        self.http.login(username="clientuser", password="pass")

        response = self.http.get(reverse("client_create"), {"return_to": "clients_list"})

        self.assertEqual(response.status_code, 403)

    def test_pool_create_by_org_assigns_org_and_access(self):
        self.http.login(username="orgadmin", password="pass")
        url = reverse("pool_create")
        payload = {
            "client": str(self.client_profile.id),
            "address": "г. Москва, ул. Тестовая 1",
            "description": "",
            "object_type": Pool.OBJECT_TYPE_POOL,
            "shape": "rect",
            "pool_type": "skimmer",
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
            "object_type": Pool.OBJECT_TYPE_POOL,
            "shape": "rect",
            "pool_type": "skimmer",
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

    def test_pool_edit_cancel_returns_to_the_edited_pool_without_referer(self):
        pool = Pool.objects.create(client=self.client_profile, address="Адрес для редактирования", organization=self.org)
        self.http.login(username="orgadmin", password="pass")

        response = self.http.get(reverse("pool_edit", kwargs={"pool_uuid": pool.uuid}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'href="{reverse("pool_detail", kwargs={"pool_uuid": pool.uuid})}" class="btn btn-outline-secondary">Отмена</a>',
            html=False,
        )

    def test_pool_create_cancel_still_returns_to_pool_list(self):
        self.http.login(username="orgadmin", password="pass")

        response = self.http.get(reverse("pool_create"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            f'href="{reverse("pool_list")}" class="btn btn-outline-secondary">Отмена</a>',
            html=False,
        )

    def test_client_staff_direct_link_has_return_to_clients_list(self):
        self.http.login(username="orgadmin", password="pass")

        response = self.http.get(reverse("client_staff", kwargs={"client_id": self.client_profile.id}))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'href="{reverse("clients_list")}"', html=False)
        self.assertContains(response, "К клиентам")
