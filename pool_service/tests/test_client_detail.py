from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from pool_service.client_crm_models import ClientCRMProfile
from pool_service.models import (
    Client,
    Organization,
    OrganizationAccess,
    Pool,
    ServiceTask,
)


class ClientDetailTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="CRM card org")
        self.owner = get_user_model().objects.create_user(
            username="crm-card-owner",
            password="test-pass",
            first_name="Александр",
        )
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.organization,
            role="owner",
        )
        self.manager = get_user_model().objects.create_user(
            username="crm-card-manager",
            password="test-pass",
            first_name="Илья",
        )
        OrganizationAccess.objects.create(
            user=self.manager,
            organization=self.organization,
            role="manager",
        )
        self.client_record = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="ООО Карточка",
            company_name="ООО Карточка",
            phone="+7 913 000-00-01",
        )
        ClientCRMProfile.objects.create(
            client=self.client_record,
            legal_form=ClientCRMProfile.LEGAL_FORM_ENTITY,
            onec_ref="15151515-1515-1515-1515-151515151515",
            onec_code="НФ-020010",
            source=ClientCRMProfile.SOURCE_ONEC,
        )
        self.pool = Pool.objects.create(
            client=self.client_record,
            organization=self.organization,
            address="Тестовый объект карточки",
        )
        self.task = ServiceTask.objects.create(
            organization=self.organization,
            title="Позвонить клиенту",
            start_date=date(2026, 10, 5),
            client=self.client_record,
            created_by=self.owner,
            primary_responsible=self.owner,
        )

    def test_owner_can_open_client_card_with_objects_and_tasks(self):
        self.client.force_login(self.owner)

        response = self.client.get(
            reverse("client_detail", args=[self.client_record.pk])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ООО Карточка")
        self.assertContains(response, "Тестовый объект карточки")
        self.assertContains(response, "Позвонить клиенту")
        self.assertContains(response, "НФ-020010")

    def test_user_without_organization_access_cannot_open_card(self):
        outsider = get_user_model().objects.create_user(
            username="crm-card-outsider",
            password="test-pass",
        )
        self.client.force_login(outsider)

        response = self.client.get(
            reverse("client_detail", args=[self.client_record.pk])
        )

        self.assertEqual(response.status_code, 403)

    def test_owner_can_update_manager_responsible_and_notes(self):
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("client_detail", args=[self.client_record.pk]),
            {
                "manager": str(self.manager.pk),
                "responsible": str(self.owner.pk),
                "notes": "Ключевой клиент.",
            },
        )

        self.assertEqual(response.status_code, 302)
        profile = ClientCRMProfile.objects.get(client=self.client_record)
        self.assertEqual(profile.manager_id, self.manager.pk)
        self.assertEqual(profile.responsible_id, self.owner.pk)
        self.assertEqual(profile.notes, "Ключевой клиент.")

    def test_manager_can_view_but_cannot_edit_profile(self):
        self.client.force_login(self.manager)

        get_response = self.client.get(
            reverse("client_detail", args=[self.client_record.pk])
        )
        post_response = self.client.post(
            reverse("client_detail", args=[self.client_record.pk]),
            {"notes": "Не должно сохраниться"},
        )

        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.status_code, 403)
        self.client_record.crm_profile.refresh_from_db()
        self.assertEqual(self.client_record.crm_profile.notes, "")
