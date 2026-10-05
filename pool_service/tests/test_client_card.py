from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from pool_service.client_crm_models import ClientCRMProfile
from pool_service.models import (
    Client,
    CrmItem,
    Organization,
    OrganizationAccess,
    Pool,
    ServiceTask,
)


class ClientCardTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="CRM card org")
        self.owner = get_user_model().objects.create_user(
            username="crm-card-owner",
            password="test-pass",
        )
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.organization,
            role="owner",
        )
        self.client.force_login(self.owner)

        self.crm_client = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="ООО Карточка",
            company_name="ООО Карточка",
            phone="+7 999 111-22-33",
            email="card@example.test",
            inn="2222000000",
        )
        ClientCRMProfile.objects.create(
            client=self.crm_client,
            onec_ref="11111111-2222-3333-4444-555555555555",
            onec_code="НФ-100",
            source=ClientCRMProfile.SOURCE_ONEC,
        )

    def test_owner_sees_client_objects_tasks_and_crm(self):
        pool = Pool.objects.create(
            organization=self.organization,
            client=self.crm_client,
            address="Тестовый объект клиента",
        )
        ServiceTask.objects.create(
            organization=self.organization,
            client=self.crm_client,
            pool=pool,
            title="Позвонить клиенту",
            start_date=date(2026, 10, 6),
            created_by=self.owner,
            primary_responsible=self.owner,
            visibility=ServiceTask.VISIBILITY_PRIVATE,
        )
        CrmItem.objects.create(
            organization=self.organization,
            direction=CrmItem.DIRECTION_SALES,
            title="Продажа оборудования",
            client=self.crm_client,
            pool=pool,
            created_by=self.owner,
        )

        response = self.client.get(
            reverse("client_detail", args=[self.crm_client.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ООО Карточка")
        self.assertContains(response, "Тестовый объект клиента")
        self.assertContains(response, "Позвонить клиенту")
        self.assertContains(response, "Продажа оборудования")
        self.assertContains(response, "НФ-100")

    def test_user_from_other_organization_is_forbidden(self):
        other_org = Organization.objects.create(name="Other org")
        other_user = get_user_model().objects.create_user(
            username="other-crm-user",
            password="test-pass",
        )
        OrganizationAccess.objects.create(
            user=other_user,
            organization=other_org,
            role="owner",
        )
        self.client.force_login(other_user)

        response = self.client.get(
            reverse("client_detail", args=[self.crm_client.pk])
        )

        self.assertEqual(response.status_code, 403)

    def test_merged_legacy_card_redirects_to_canonical_client(self):
        legacy = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="Старая карточка",
        )
        ClientCRMProfile.objects.create(
            client=legacy,
            merged_into=self.crm_client,
        )

        response = self.client.get(reverse("client_detail", args=[legacy.pk]))

        self.assertRedirects(
            response,
            reverse("client_detail", args=[self.crm_client.pk]),
            fetch_redirect_response=False,
        )

    def test_owner_can_save_manager_responsible_and_notes(self):
        employee = get_user_model().objects.create_user(
            username="crm-manager",
            first_name="Иван",
            last_name="Менеджеров",
        )
        OrganizationAccess.objects.create(
            user=employee,
            organization=self.organization,
            role="manager",
        )

        response = self.client.post(
            reverse("client_detail", args=[self.crm_client.pk]),
            {
                "action": "save_profile",
                "manager": employee.pk,
                "responsible": employee.pk,
                "notes": "Позвонить после поставки.",
            },
        )

        self.assertRedirects(
            response,
            reverse("client_detail", args=[self.crm_client.pk]),
            fetch_redirect_response=False,
        )
        profile = ClientCRMProfile.objects.get(client=self.crm_client)
        self.assertEqual(profile.manager_id, employee.pk)
        self.assertEqual(profile.responsible_id, employee.pk)
        self.assertEqual(profile.notes, "Позвонить после поставки.")


    def test_task_create_prefills_client_from_card(self):
        response = self.client.get(
            reverse("task_create"),
            {"client": self.crm_client.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            str(response.context["form"]["client"].value()),
            str(self.crm_client.pk),
        )
