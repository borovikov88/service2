from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from pool_service.client_crm_models import ClientCRMProfile
from pool_service.models import Client, Organization, OrganizationAccess


class ClientMergeSearchTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Merge search org")
        self.user = get_user_model().objects.create_user(
            username="merge-search-owner",
            password="test-pass",
        )
        OrganizationAccess.objects.create(
            user=self.user,
            organization=self.organization,
            role="owner",
        )
        self.client.force_login(self.user)

    def create_onec_client(self, name, phone="", inn=""):
        client = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name=name,
            company_name=name,
            phone=phone,
            inn=inn,
        )
        ClientCRMProfile.objects.create(
            client=client,
            onec_ref=f"{client.pk:08d}-0000-0000-0000-000000000000",
            source=ClientCRMProfile.SOURCE_ONEC,
        )
        return client

    def test_live_search_returns_imported_clients(self):
        target = self.create_onec_client(
            "Канонический клиент",
            phone="+7 999 123-45-67",
            inn="2222000000",
        )
        self.create_onec_client("Другой клиент")

        with override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.pk)
        ):
            response = self.client.get(
                reverse("client_merge_search"),
                {"q": "Канонич"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["id"] for item in payload["results"]], [target.pk])
        self.assertEqual(payload["results"][0]["inn"], "2222000000")

    def test_live_search_excludes_merged_legacy_cards(self):
        target = self.create_onec_client("Основной клиент")
        merged = self.create_onec_client("Архивный клиент")
        merged_profile = merged.crm_profile
        merged_profile.merged_into = target
        merged_profile.save(update_fields=["merged_into", "updated_at"])

        with override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.pk)
        ):
            response = self.client.get(
                reverse("client_merge_search"),
                {"q": "Архивный"},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["results"], [])
