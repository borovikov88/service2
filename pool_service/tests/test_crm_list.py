from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.models import Client, CrmItem, Organization, OrganizationAccess, Pool


class CrmListTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="CRM Org",
            city="Барнаул",
            trial_started_at=timezone.now(),
        )
        self.admin = User.objects.create_user(username="crm_admin", password="pass")
        self.manager = User.objects.create_user(username="crm_manager", password="pass", first_name="Илья", last_name="Шукшин")
        self.service = User.objects.create_user(username="crm_service", password="pass", first_name="Александр", last_name="Боровиков")
        OrganizationAccess.objects.create(user=self.admin, organization=self.organization, role="admin")
        OrganizationAccess.objects.create(user=self.manager, organization=self.organization, role="manager")
        OrganizationAccess.objects.create(user=self.service, organization=self.organization, role="service")

        self.client_record = Client.objects.create(
            organization=self.organization,
            client_type="private",
            first_name="Тест",
            last_name="Клиент",
            name="Школа № 137",
            phone="+79000000000",
        )
        self.pool = Pool.objects.create(
            client=self.client_record,
            address="ул. Шумакова, 78",
            organization=self.organization,
        )

        self.item_a = CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title='Заявка: "Насос"',
            client=self.client_record,
            pool=self.pool,
            stage=CrmItem.STAGE_SERVICE_NEW,
            responsible=self.manager,
            created_by=self.service,
        )
        self.item_b = CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title='Заявка: "Форсунка"',
            client=self.client_record,
            pool=self.pool,
            stage=CrmItem.STAGE_SERVICE_IN_PROGRESS,
            responsible=self.service,
            created_by=self.admin,
        )

    def test_crm_list_filters_by_responsible(self):
        self.client.force_login(self.admin)

        response = self.client.get(
            reverse("crm_list", kwargs={"direction": CrmItem.DIRECTION_SERVICE}),
            {"responsible": str(self.manager.id)},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Насос")
        self.assertNotContains(response, "Форсунка")
        self.assertContains(response, "Все сотрудники")

    def crm_payload(self, *, title='Заявка: "Обновлённый насос"'):
        return {
            "title": title,
            "amount": "1250.00",
            "client": str(self.client_record.id),
            "pool": str(self.pool.id),
            "stage": CrmItem.STAGE_SERVICE_IN_PROGRESS,
            "urgency": CrmItem.URGENCY_REQUIRED,
            "responsible": str(self.manager.id),
            "photo_url": "",
            "description": "Проверка возврата",
            "service_works": "",
            "equipment_replacement": "",
        }

    def test_crm_list_detail_edit_returns_to_same_direction_list(self):
        self.client.force_login(self.admin)
        direction = CrmItem.DIRECTION_SERVICE
        list_url = reverse("crm_list", kwargs={"direction": direction})
        detail_url = reverse("crm_view", kwargs={"direction": direction, "item_id": self.item_a.id})
        edit_url = reverse("crm_edit", kwargs={"direction": direction, "item_id": self.item_a.id})

        listing = self.client.get(list_url, {"responsible": "__all__"})
        self.assertContains(listing, f'data-crm-row-url="{detail_url}?return_to=crm_list"', html=False)

        detail = self.client.get(detail_url, {"return_to": "crm_list"})
        self.assertEqual(detail.context["crm_edit_url"], f"{edit_url}?return_to=crm_list")

        edit = self.client.get(edit_url, {"return_to": "crm_list"})
        self.assertEqual(edit.context["return_url"], list_url)
        self.assertContains(edit, 'name="return_to" value="crm_list"')
        self.assertContains(edit, f'href="{list_url}"')

        invalid = self.client.post(edit_url, {"title": "", "return_to": "crm_list"})
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.context["return_url"], list_url)
        self.assertContains(invalid, 'name="return_to" value="crm_list"')

        saved = self.client.post(edit_url, {**self.crm_payload(), "return_to": "crm_list"})
        self.assertRedirects(saved, list_url)

    def test_crm_detail_edit_returns_to_same_card(self):
        self.client.force_login(self.admin)
        direction = CrmItem.DIRECTION_SERVICE
        detail_url = reverse("crm_view", kwargs={"direction": direction, "item_id": self.item_a.id})
        edit_url = reverse("crm_edit", kwargs={"direction": direction, "item_id": self.item_a.id})

        detail = self.client.get(detail_url)
        self.assertEqual(detail.context["crm_edit_url"], f"{edit_url}?return_to=crm_detail")

        edit = self.client.get(edit_url, {"return_to": "crm_detail"})
        self.assertEqual(edit.context["return_url"], detail_url)
        self.assertContains(edit, 'name="return_to" value="crm_detail"')
        self.assertContains(edit, f'href="{detail_url}"')

        saved = self.client.post(edit_url, {**self.crm_payload(), "return_to": "crm_detail"})
        self.assertRedirects(saved, detail_url)

    def test_crm_edit_direct_and_forged_context_fall_back_to_list(self):
        self.client.force_login(self.admin)
        direction = CrmItem.DIRECTION_SERVICE
        list_url = reverse("crm_list", kwargs={"direction": direction})
        edit_url = reverse("crm_edit", kwargs={"direction": direction, "item_id": self.item_a.id})

        for token in (None, "https://example.invalid/", "pool_detail"):
            response = self.client.get(edit_url, {} if token is None else {"return_to": token})
            self.assertEqual(response.context["return_url"], list_url)
            self.assertNotContains(response, "example.invalid")

        saved = self.client.post(
            edit_url,
            {**self.crm_payload(), "return_to": "https://example.invalid/"},
        )
        self.assertRedirects(saved, list_url)

    def test_crm_view_edit_shortcut_keeps_only_allowed_token(self):
        self.client.force_login(self.admin)
        direction = CrmItem.DIRECTION_SERVICE
        detail_url = reverse("crm_view", kwargs={"direction": direction, "item_id": self.item_a.id})
        edit_url = reverse("crm_edit", kwargs={"direction": direction, "item_id": self.item_a.id})

        for token in ("crm_list", "crm_detail"):
            response = self.client.get(detail_url, {"edit": "1", "return_to": token})
            self.assertRedirects(response, f"{edit_url}?return_to={token}")

        for token in (None, "https://example.invalid/"):
            query = {"edit": "1"}
            if token is not None:
                query["return_to"] = token
            response = self.client.get(detail_url, query)
            self.assertRedirects(response, f"{edit_url}?return_to=crm_list")

    def test_crm_create_remains_list_flow_without_return_token(self):
        self.client.force_login(self.admin)
        direction = CrmItem.DIRECTION_SERVICE
        list_url = reverse("crm_list", kwargs={"direction": direction})
        create_url = reverse("crm_create", kwargs={"direction": direction})

        form = self.client.get(create_url)
        self.assertEqual(form.status_code, 200)
        self.assertNotContains(form, 'name="return_to"')
        self.assertContains(form, f'href="{list_url}"')

        created = self.client.post(create_url, self.crm_payload(title='Заявка: "Новая"'))
        self.assertRedirects(created, list_url)
        self.assertTrue(CrmItem.objects.filter(title='Заявка: "Новая"').exists())

    def test_crm_edit_keeps_existing_organization_and_direction_guards(self):
        foreign_organization = Organization.objects.create(name="Foreign CRM Org", city="Барнаул")
        foreign_item = CrmItem.objects.create(
            organization=foreign_organization,
            direction=CrmItem.DIRECTION_SERVICE,
            title="Чужая заявка",
            stage=CrmItem.STAGE_SERVICE_NEW,
        )
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse("crm_edit", kwargs={"direction": CrmItem.DIRECTION_SERVICE, "item_id": foreign_item.id}),
            {"return_to": "crm_detail"},
        )
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.service)
        for direction in (
            CrmItem.DIRECTION_PROJECT,
            CrmItem.DIRECTION_SALES,
            CrmItem.DIRECTION_TENDER,
        ):
            self.assertEqual(self.client.get(reverse("crm_list", kwargs={"direction": direction})).status_code, 403)
