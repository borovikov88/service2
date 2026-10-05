from django.contrib.auth import get_user_model
from django.test import TestCase

from pool_service.client_crm_models import (
    ClientCRMProfile,
    ClientCompanyLink,
    ClientContact,
    ClientImportCandidate,
    ClientImportRun,
)
from pool_service.client_merge import merge_clients
from pool_service.models import Client, ClientAccess, Organization, Pool


class ClientMergeTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Аквалайн merge test")
        self.legacy = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="Старая карточка",
            phone="+7 913 000-00-01",
        )
        self.target = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="Каноническая карточка",
        )
        ClientCRMProfile.objects.create(
            client=self.target,
            onec_ref="77777777-7777-7777-7777-777777777777",
            source=ClientCRMProfile.SOURCE_ONEC,
        )

    def merge(self, source=None, target=None):
        return merge_clients(
            (source or self.legacy).pk,
            (target or self.target).pk,
            organization_id=self.organization.pk,
        )

    def test_merge_moves_pool_contacts_and_marks_legacy_profile(self):
        pool = Pool.objects.create(
            client=self.legacy,
            organization=self.organization,
            address="Тестовый объект",
        )
        ClientContact.objects.create(
            client=self.legacy,
            kind=ClientContact.KIND_PHONE,
            value="+7 913 000-00-01",
            match_value="79130000001",
            is_primary=True,
        )

        result = self.merge()

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

    def test_merge_deduplicates_contacts_by_normalized_identity(self):
        ClientContact.objects.create(
            client=self.legacy,
            kind=ClientContact.KIND_PHONE,
            value="+7 913 000-00-01",
            match_value="79130000001",
            is_primary=True,
            sources=["manual"],
        )
        ClientContact.objects.create(
            client=self.target,
            kind=ClientContact.KIND_PHONE,
            value="8 (913) 000-00-01",
            match_value="79130000001",
            sources=["onec"],
        )

        self.merge()

        contacts = ClientContact.objects.filter(
            client=self.target,
            kind=ClientContact.KIND_PHONE,
            match_value="79130000001",
        )
        self.assertEqual(contacts.count(), 1)
        contact = contacts.get()
        self.assertTrue(contact.is_primary)
        self.assertEqual(set(contact.sources), {"manual", "onec"})

    def test_merge_preserves_company_link_metadata_on_collision(self):
        person = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Контакт",
        )
        ClientCompanyLink.objects.create(
            company=self.legacy,
            person=person,
            roles=["accountant"],
            position="Бухгалтер",
            is_primary=True,
            source=ClientCompanyLink.SOURCE_MANUAL,
        )
        ClientCompanyLink.objects.create(
            company=self.target,
            person=person,
            roles=["director"],
            source=ClientCompanyLink.SOURCE_ONEC_IP,
            source_reference="ref",
            automatic=True,
        )

        self.merge()

        link = ClientCompanyLink.objects.get(company=self.target, person=person)
        self.assertEqual(set(link.roles), {"accountant", "director"})
        self.assertEqual(link.position, "Бухгалтер")
        self.assertTrue(link.is_primary)
        self.assertTrue(link.automatic)
        self.assertEqual(link.source_reference, "ref")
        self.assertEqual(
            ClientCompanyLink.objects.filter(company=self.legacy).count(),
            0,
        )

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

        self.merge()

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
        with self.assertRaisesMessage(
            ValueError,
            "Целевая карточка должна быть импортирована из 1С",
        ):
            self.merge(target=other)

    def test_merge_rejects_conflicting_user_accounts(self):
        user_a = get_user_model().objects.create_user(username="legacy-user")
        user_b = get_user_model().objects.create_user(username="target-user")
        self.legacy.user = user_a
        self.legacy.save(update_fields=["user"])
        self.target.user = user_b
        self.target.save(update_fields=["user"])

        with self.assertRaisesMessage(ValueError, "разные пользовательские аккаунты"):
            self.merge()

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
            self.merge()

    def test_merge_is_scoped_to_authorized_organization(self):
        other_org = Organization.objects.create(name="Other org")
        other_source = Client.objects.create(
            organization=other_org,
            client_type="legal",
            name="Other legacy",
        )
        other_target = Client.objects.create(
            organization=other_org,
            client_type="legal",
            name="Other target",
        )
        ClientCRMProfile.objects.create(
            client=other_target,
            onec_ref="dddddddd-dddd-dddd-dddd-dddddddddddd",
            source=ClientCRMProfile.SOURCE_ONEC,
        )

        with self.assertRaisesMessage(ValueError, "Карточка клиента не найдена"):
            merge_clients(
                other_source.pk,
                other_target.pk,
                organization_id=self.organization.pk,
            )

    def test_merge_is_blocked_during_active_import_run(self):
        ClientImportRun.objects.create(
            organization=self.organization,
            status=ClientImportRun.STATUS_RUNNING,
        )
        with self.assertRaisesMessage(ValueError, "Дождитесь завершения"):
            self.merge()

    def test_merge_clears_unrelated_candidate_match(self):
        candidate = ClientImportCandidate.objects.create(
            organization=self.organization,
            source_ref="eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee",
            source_code="TEST-1",
            source_kind=ClientImportCandidate.KIND_LEGAL,
            name="Другой кандидат",
            status=ClientImportCandidate.STATUS_READY,
            matched_client=self.legacy,
        )

        self.merge()

        candidate.refresh_from_db()
        self.assertIsNone(candidate.matched_client)
        self.assertEqual(candidate.status, ClientImportCandidate.STATUS_REVIEW)
        self.assertIn("повторное сопоставление", candidate.reason)
