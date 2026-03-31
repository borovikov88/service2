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
