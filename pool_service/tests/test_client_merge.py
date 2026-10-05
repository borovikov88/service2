from django.contrib.auth import get_user_model
from django.test import TestCase

from pool_service.client_crm_models import ClientCRMProfile, ClientContact
from pool_service.client_merge import merge_clients
from pool_service.models import Client, ClientAccess, Organization, Pool


class ClientMergeTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Аквалайн merge test")
        self.legacy = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="Школа 133",
            phone="+7 913 000-00-01",
        )
        self.target = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name='МБОУ "Школа №133"',
        )
        ClientCRMProfile.objects.create(
            client=self.target,
            onec_ref="77777777-7777-7777-7777-777777777777",
            source=ClientCRMProfile.SOURCE_ONEC,
        )

    def test_merge_moves_pool_contacts_and_marks_legacy_profile(self):
        pool = Pool.objects.create(
            client=self.legacy,
            organization=self.organization,
            address="Барнаул, объект школы 133",
        )
        ClientContact.objects.create(
            client=self.legacy,
            kind=ClientContact.KIND_PHONE,
            value="+7 913 000-00-01",
            match_value="79130000001",
            is_primary=True,
        )

        result = merge_clients(
            self.legacy.pk,
            self.target.pk,
            organization_id=self.organization.pk,
        )

        pool.refresh_from_db()
        self.target.refresh_from_db()
        legacy_profile = ClientCRMProfile.objects.get(client=self.legacy)
        self.assertEqual(pool.client_id, self.target.pk)
        self.assertEqual(self.target.phone, "+7 913 000-00-01")
        self.assertEqual(legacy_profile.merged_into_id, self.target.pk)
        self.assertIsNotNone(legacy_profile.merged_at)
        self.assertTrue(
            ClientContact.objects.filter(
                client=self.target,
                match_value="79130000001",
            ).exists()
        )
        self.assertEqual(result["moved"].get("Pool"), 1)
        self.assertTrue(Client.objects.filter(pk=self.legacy.pk).exists())

    def test_merge_deduplicates_client_staff_access(self):
        user = get_user_model().objects.create_user(username="pool-staff")
        ClientAccess.objects.create(
            user=user,
            client=self.legacy,
            role="editor",
            phone="79990000000",
        )
        ClientAccess.objects.create(
            user=user,
            client=self.target,
            role="viewer",
            phone="",
        )

        merge_clients(
            self.legacy.pk,
            self.target.pk,
            organization_id=self.organization.pk,
        )

        accesses = ClientAccess.objects.filter(user=user)
        self.assertEqual(accesses.count(), 1)
        access = accesses.get()
        self.assertEqual(access.client_id, self.target.pk)
        self.assertEqual(access.role, "editor")
        self.assertEqual(access.phone, "79990000000")

    def test_merge_rejects_non_onec_target(self):
        other = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="Не импортирован",
        )
        with self.assertRaisesMessage(ValueError, "Целевая карточка должна быть импортирована из 1С"):
            merge_clients(
                self.legacy.pk,
                other.pk,
                organization_id=self.organization.pk,
            )

    def test_merge_rejects_conflicting_user_accounts(self):
        user_a = get_user_model().objects.create_user(username="legacy-user")
        user_b = get_user_model().objects.create_user(username="target-user")
        self.legacy.user = user_a
        self.legacy.save(update_fields=["user"])
        self.target.user = user_b
        self.target.save(update_fields=["user"])

        with self.assertRaisesMessage(ValueError, "разные пользовательские аккаунты"):
            merge_clients(
            self.legacy.pk,
            self.target.pk,
            organization_id=self.organization.pk,
        )


    def test_merge_rejects_target_that_is_already_merged(self):
        other_target = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="Итоговая карточка",
        )
        ClientCRMProfile.objects.create(
            client=other_target,
            onec_ref="cccccccc-cccc-cccc-cccc-cccccccccccc",
            source=ClientCRMProfile.SOURCE_ONEC,
        )
        target_profile = ClientCRMProfile.objects.get(client=self.target)
        target_profile.merged_into = other_target
        target_profile.save(update_fields=["merged_into", "updated_at"])

        with self.assertRaisesMessage(ValueError, "Целевая карточка уже объединена"):
            merge_clients(
            self.legacy.pk,
            self.target.pk,
            organization_id=self.organization.pk,
        )
