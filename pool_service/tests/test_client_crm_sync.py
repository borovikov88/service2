from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service.client_crm_import import (
    _contact_rows,
    apply_candidate,
    apply_ready_candidates,
)
from pool_service.client_crm_models import (
    ClientCRMProfile,
    ClientCompanyLink,
    ClientContact,
    ClientImportCandidate,
    ClientImportRun,
)
from pool_service.client_crm_sync import (
    sync_all_onec_clients,
    sync_recent_onec_clients,
)
from pool_service.models import Client, Organization


class ClientCRMAutoSyncTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="CRM sync test")

    def _row(self, **changes):
        row = {
            "Ref_Key": "12345678-1234-1234-1234-1234567890ab",
            "Code": "C-200",
            "Description": "Sync Client",
            "НаименованиеПолное": "Sync Client",
            "ЮридическоеФизическоеЛицо": "ФизическоеЛицо",
            "ВидКонтрагента": "ФизическоеЛицо",
            "ИНН": "",
            "КПП": "",
            "ФИО": "Sync Client",
            "ДатаРождения": "0001-01-01T00:00:00",
            "ДатаСоздания": "2026-10-05T00:00:00",
            "НомерТелефонаДляПоиска": "",
            "АдресЭПДляПоиска": "",
            "Покупатель": True,
            "Недействителен": False,
        }
        row.update(changes)
        return row

    def _sync(self, rows):
        with override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.pk)
        ), patch(
            "pool_service.client_crm_sync.config_from_settings",
            return_value=object(),
        ), patch(
            "pool_service.client_crm_sync.fetch_metadata",
            return_value=b"metadata",
        ), patch(
            "pool_service.client_crm_sync.query_1c_rows",
            return_value={"rows": rows, "complete": True},
        ), patch(
            "pool_service.client_crm_sync._contact_rows",
            return_value={},
        ):
            return sync_recent_onec_clients(lookback_hours=48)

    def test_recent_private_buyer_is_imported(self):
        row = self._row()
        result = self._sync([row])

        self.assertEqual(result["imported"], 1)
        client = Client.objects.get(
            organization=self.organization,
            crm_profile__onec_ref=row["Ref_Key"],
        )
        self.assertEqual(client.client_type, "private")

    def test_legal_without_inn_waits_for_review(self):
        row = self._row(
            Ref_Key="22345678-1234-1234-1234-1234567890ab",
            Description="Legal Missing Tax Id",
            НаименованиеПолное="Legal Missing Tax Id",
            ЮридическоеФизическоеЛицо="ЮридическоеЛицо",
            ВидКонтрагента="ЮридическоеЛицо",
            ФИО="",
        )
        result = self._sync([row])

        self.assertEqual(result["review"], 1)
        candidate = ClientImportCandidate.objects.get(source_ref=row["Ref_Key"])
        self.assertEqual(candidate.status, ClientImportCandidate.STATUS_REVIEW)

    def test_recent_sync_refreshes_linked_client(self):
        ref = "32345678-1234-1234-1234-1234567890ab"
        client = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Old Name",
        )
        ClientCRMProfile.objects.create(
            client=client,
            onec_ref=ref,
            source=ClientCRMProfile.SOURCE_ONEC,
        )
        ClientImportCandidate.objects.create(
            organization=self.organization,
            source_ref=ref,
            source_code="C-202",
            source_kind=ClientImportCandidate.KIND_PRIVATE,
            name="Old Name",
            status=ClientImportCandidate.STATUS_IMPORTED,
            matched_client=client,
            applied_at=timezone.now(),
        )
        row = self._row(
            Ref_Key=ref,
            Code="C-202",
            Description="New Name",
            НаименованиеПолное="New Name",
            ФИО="New Name",
        )
        result = self._sync([row])

        client.refresh_from_db()
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(client.name, "New Name")

    def test_refresh_preserves_crm_name_changed_manually(self):
        ref = "42345678-1234-1234-1234-1234567890ab"
        client = Client.objects.create(
            organization=self.organization, client_type="private",
            name="Дыченко Александр", first_name="Надежда", last_name="Дыченко",
            phone="+7 999 123-45-67",
        )
        ClientCRMProfile.objects.create(
            client=client, onec_ref=ref, onec_name="Дыченко Надежда Николаевна",
            middle_name="Николаевна", source=ClientCRMProfile.SOURCE_ONEC,
        )
        ClientImportCandidate.objects.create(
            organization=self.organization, source_ref=ref, source_code="C-203",
            source_kind=ClientImportCandidate.KIND_PRIVATE,
            name="Дыченко Надежда Николаевна", status=ClientImportCandidate.STATUS_IMPORTED,
            matched_client=client, applied_at=timezone.now(),
            phone="+7 999 123-45-67",
        )
        row = self._row(
            Ref_Key=ref, Code="C-203", Description="Дыченко Надежда Николаевна",
            ФИО="Дыченко Надежда Николаевна",
            НомерТелефонаДляПоиска="+7 999 123-45-67",
        )
        self._sync([row])
        client.refresh_from_db()
        self.assertEqual(client.name, "Дыченко Александр")
        self.assertEqual(client.first_name, "Надежда")
        self.assertEqual(client.phone, "+7 999 123 4567")

    def test_ip_name_only_does_not_auto_merge(self):
        Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Test Person",
        )
        candidate = ClientImportCandidate.objects.create(
            organization=self.organization,
            source_ref="42345678-1234-1234-1234-1234567890ab",
            source_code="C-203",
            source_kind=ClientImportCandidate.KIND_IP,
            name="Test Person",
            legal_name="IP Test Person",
            inn="TAX-ID",
            status=ClientImportCandidate.STATUS_READY,
            payload={"fio": "Test Person", "phones": [], "emails": []},
        )
        result = apply_ready_candidates(self.organization)

        candidate.refresh_from_db()
        self.assertEqual(result, {"imported": 0, "failed": 1})
        self.assertEqual(candidate.status, ClientImportCandidate.STATUS_REVIEW)
        self.assertFalse(ClientCompanyLink.objects.exists())


    def test_refresh_removes_stale_onec_contact_but_keeps_manual_contact(self):
        ref = "52345678-1234-1234-1234-1234567890ab"
        client = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Contact Sync",
            phone="10001",
        )
        ClientCRMProfile.objects.create(
            client=client,
            onec_ref=ref,
            source=ClientCRMProfile.SOURCE_ONEC,
        )
        ClientImportCandidate.objects.create(
            organization=self.organization,
            source_ref=ref,
            source_code="C-204",
            source_kind=ClientImportCandidate.KIND_PRIVATE,
            name="Contact Sync",
            phone="10001",
            status=ClientImportCandidate.STATUS_IMPORTED,
            matched_client=client,
            applied_at=timezone.now(),
        )
        ClientContact.objects.create(
            client=client,
            kind=ClientContact.KIND_PHONE,
            value="10001",
            match_value="10001",
            sources=["onec"],
            source_reference=ref,
        )
        manual = ClientContact.objects.create(
            client=client,
            kind=ClientContact.KIND_PHONE,
            value="20002",
            match_value="20002",
            sources=["manual", "onec"],
            source_reference=ref,
        )

        row = self._row(
            Ref_Key=ref,
            Code="C-204",
            Description="Contact Sync",
            НаименованиеПолное="Contact Sync",
            ФИО="Contact Sync",
            НомерТелефонаДляПоиска="",
        )
        result = self._sync([row])

        client.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(result["refreshed"], 1)
        self.assertIsNone(client.phone)
        self.assertFalse(
            ClientContact.objects.filter(
                client=client,
                match_value="10001",
            ).exists()
        )
        self.assertEqual(manual.sources, ["manual"])
        self.assertEqual(manual.source_reference, "")


    def test_incremental_sync_skips_active_manual_run(self):
        ClientImportRun.objects.create(
            organization=self.organization,
            status=ClientImportRun.STATUS_RUNNING,
        )

        with override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.pk)
        ), patch(
            "pool_service.client_crm_sync.config_from_settings"
        ) as config_mock:
            result = sync_recent_onec_clients()

        self.assertEqual(result, {"skipped_active_run": 1})
        config_mock.assert_not_called()

    def test_incremental_sync_recovers_stale_run(self):
        run = ClientImportRun.objects.create(
            organization=self.organization,
            status=ClientImportRun.STATUS_RUNNING,
        )
        ClientImportRun.objects.filter(pk=run.pk).update(
            updated_at=timezone.now() - timedelta(hours=3)
        )

        result = self._sync([])

        run.refresh_from_db()
        self.assertEqual(result, {})
        self.assertEqual(run.status, ClientImportRun.STATUS_FAILED)
        self.assertIn("зависший импорт", run.error)

    def test_full_sync_uses_visible_import_run(self):
        with override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.pk)
        ), patch(
            "pool_service.client_crm_sync.scan_onec_clients",
            return_value={"total": 0},
        ), patch(
            "pool_service.client_crm_sync.apply_ready_candidates",
            return_value={"imported": 0, "failed": 0},
        ):
            result = sync_all_onec_clients()

        run = ClientImportRun.objects.get(organization=self.organization)
        self.assertEqual(run.status, ClientImportRun.STATUS_SUCCESS)
        self.assertEqual(result["scan"], {"total": 0})
        self.assertEqual(result["apply"], {"imported": 0, "failed": 0})


    def test_private_name_only_conflict_waits_for_review(self):
        Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Sync Client",
        )

        result = self._sync([self._row()])

        self.assertEqual(result["review"], 1)
        self.assertEqual(
            Client.objects.filter(
                organization=self.organization,
                client_type="private",
                name="Sync Client",
            ).count(),
            1,
        )
        candidate = ClientImportCandidate.objects.get(
            source_ref="12345678-1234-1234-1234-1234567890ab"
        )
        self.assertEqual(candidate.status, ClientImportCandidate.STATUS_REVIEW)
        self.assertIn("нет телефона", candidate.reason)


    def test_phone_match_cannot_replace_different_onec_identity(self):
        existing_ref = "62345678-1234-1234-1234-1234567890ab"
        new_ref = "72345678-1234-1234-1234-1234567890ab"
        existing = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Existing Identity",
            phone="+7 999 300-00-03",
        )
        ClientCRMProfile.objects.create(
            client=existing,
            onec_ref=existing_ref,
            source=ClientCRMProfile.SOURCE_ONEC,
        )

        row = self._row(
            Ref_Key=new_ref,
            Code="C-205",
            Description="Different Identity",
            НаименованиеПолное="Different Identity",
            ФИО="Different Identity",
            НомерТелефонаДляПоиска="+7 999 300-00-03",
        )
        result = self._sync([row])

        existing.refresh_from_db()
        existing.crm_profile.refresh_from_db()
        self.assertEqual(result["review"], 1)
        self.assertEqual(existing.crm_profile.onec_ref, existing_ref)
        candidate = ClientImportCandidate.objects.get(source_ref=new_ref)
        self.assertEqual(
            candidate.status,
            ClientImportCandidate.STATUS_DUPLICATE,
        )
        self.assertIsNone(candidate.matched_client)


    def test_incomplete_contact_batch_aborts_reconciliation(self):
        with patch(
            "pool_service.client_crm_import.query_1c_rows",
            return_value={"rows": [], "complete": False},
        ):
            with self.assertRaisesMessage(
                RuntimeError,
                "contact batch is incomplete",
            ):
                _contact_rows(
                    object(),
                    ["12345678-1234-1234-1234-1234567890ab"],
                    b"metadata",
                )

    def test_full_sync_does_not_apply_ready_candidate_missing_from_snapshot(self):
        stale_candidate = ClientImportCandidate.objects.create(
            organization=self.organization,
            source_ref="82345678-1234-1234-1234-1234567890ab",
            source_code="C-206",
            source_kind=ClientImportCandidate.KIND_PRIVATE,
            name="Removed 1C Buyer",
            status=ClientImportCandidate.STATUS_READY,
        )

        with override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.pk)
        ), patch(
            "pool_service.client_crm_sync.scan_onec_clients",
            return_value={"total": 0, "_seen_refs": []},
        ):
            result = sync_all_onec_clients()

        stale_candidate.refresh_from_db()
        self.assertEqual(stale_candidate.status, ClientImportCandidate.STATUS_READY)
        self.assertIsNone(stale_candidate.applied_at)
        self.assertEqual(result["apply"]["imported"], 0)

    def test_existing_ip_link_refreshes_person_scalars(self):
        ref = "92345678-1234-1234-1234-1234567890ab"
        company = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="IP Old",
            company_name="IP Old",
        )
        ClientCRMProfile.objects.create(
            client=company,
            onec_ref=ref,
            source=ClientCRMProfile.SOURCE_ONEC,
            legal_form=ClientCRMProfile.LEGAL_FORM_IP,
        )
        person = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Old Person",
            first_name="Old",
            last_name="Person",
            phone="+7 999 111-11-11",
            email="old@example.test",
        )
        person_profile = ClientCRMProfile.objects.create(
            client=person,
            source=ClientCRMProfile.SOURCE_ONEC_IP,
        )
        ClientCompanyLink.objects.create(
            company=company,
            person=person,
            source=ClientCompanyLink.SOURCE_ONEC_IP,
            source_reference=ref,
            automatic=True,
            is_primary=True,
        )
        candidate = ClientImportCandidate.objects.create(
            organization=self.organization,
            source_ref=ref,
            source_code="C-207",
            source_kind=ClientImportCandidate.KIND_IP,
            name="ИП Иванов Иван Иванович",
            legal_name="ИП Иванов Иван Иванович",
            phone="+7 999 222-22-22",
            email="new@example.test",
            birth_date=timezone.localdate(),
            status=ClientImportCandidate.STATUS_IMPORTED,
            matched_client=company,
            applied_at=timezone.now(),
            payload={
                "fio": "Иванов Иван Иванович",
                "phones": [
                    {
                        "value": "+7 999 222-22-22",
                        "match": "79992222222",
                        "label": "Основной",
                    }
                ],
                "emails": [
                    {
                        "value": "new@example.test",
                        "match": "new@example.test",
                        "label": "Основной",
                    }
                ],
            },
        )

        apply_candidate(candidate)

        person.refresh_from_db()
        person_profile.refresh_from_db()
        self.assertEqual(person.name, "Иванов Иван Иванович")
        self.assertEqual(person.first_name, "Иван")
        self.assertEqual(person.last_name, "Иванов")
        self.assertEqual(person.phone, "+7 999 222 2222")
        self.assertEqual(person.email, "new@example.test")
        self.assertEqual(person_profile.middle_name, "Иванович")
        self.assertEqual(person_profile.birth_date, candidate.birth_date)

    def test_same_onec_ref_reconciles_client_type_and_removes_stale_ip_link(self):
        ref = "a2345678-1234-1234-1234-1234567890ab"
        company = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="Old IP",
            company_name="Old IP",
        )
        ClientCRMProfile.objects.create(
            client=company,
            onec_ref=ref,
            source=ClientCRMProfile.SOURCE_ONEC,
            legal_form=ClientCRMProfile.LEGAL_FORM_IP,
        )
        person = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Linked Person",
        )
        ClientCompanyLink.objects.create(
            company=company,
            person=person,
            source=ClientCompanyLink.SOURCE_ONEC_IP,
            source_reference=ref,
            automatic=True,
        )
        candidate = ClientImportCandidate.objects.create(
            organization=self.organization,
            source_ref=ref,
            source_code="C-208",
            source_kind=ClientImportCandidate.KIND_PRIVATE,
            name="Петров Петр Петрович",
            legal_name="",
            status=ClientImportCandidate.STATUS_IMPORTED,
            matched_client=company,
            applied_at=timezone.now(),
            payload={"fio": "Петров Петр Петрович", "phones": [], "emails": []},
        )

        apply_candidate(candidate)

        company.refresh_from_db()
        company.crm_profile.refresh_from_db()
        self.assertEqual(company.client_type, "private")
        self.assertIsNone(company.company_name)
        self.assertEqual(company.first_name, "Петр")
        self.assertEqual(company.last_name, "Петров")
        self.assertEqual(
            company.crm_profile.legal_form,
            ClientCRMProfile.LEGAL_FORM_NONE,
        )
        self.assertFalse(
            ClientCompanyLink.objects.filter(
                company=company,
                source=ClientCompanyLink.SOURCE_ONEC_IP,
                source_reference=ref,
            ).exists()
        )
