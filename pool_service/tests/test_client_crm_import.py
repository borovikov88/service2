from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from pool_service.client_crm_import import (
    apply_candidate,
    apply_ready_candidates,
    request_client_import_scan,
    resolve_import_candidate,
    scan_onec_clients,
)
from pool_service.client_crm_models import (
    ClientCRMProfile,
    ClientCompanyLink,
    ClientContact,
    ClientImportCandidate,
    ClientImportRun,
)
from pool_service.models import Client, Organization


class ClientCRMImportTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Аквалайн CRM test")

    def candidate(self, **overrides):
        data = {
            "organization": self.organization,
            "source_ref": "11111111-1111-1111-1111-111111111111",
            "source_code": "C-1",
            "source_kind": ClientImportCandidate.KIND_IP,
            "name": "Иванов Иван Иванович ИП",
            "legal_name": "ИП Иванов Иван Иванович",
            "inn": "220000000001",
            "phone": "+7 913 000-00-01",
            "email": "ivanov@example.test",
            "payload": {
                "fio": "Иванов Иван Иванович",
                "phones": [{"value": "+7 913 000-00-01", "match": "79130000001", "label": "Основной"}],
                "emails": [{"value": "ivanov@example.test", "match": "ivanov@example.test", "label": "Основной"}],
            },
            "status": ClientImportCandidate.STATUS_READY,
        }
        data.update(overrides)
        return ClientImportCandidate.objects.create(**data)

    def test_ip_import_creates_company_person_and_automatic_link(self):
        candidate = self.candidate()

        company = apply_candidate(candidate)

        self.assertEqual(company.client_type, "legal")
        self.assertEqual(company.inn, "220000000001")
        self.assertEqual(company.crm_profile.legal_form, ClientCRMProfile.LEGAL_FORM_IP)
        link = ClientCompanyLink.objects.get(company=company)
        self.assertTrue(link.automatic)
        self.assertEqual(link.source, ClientCompanyLink.SOURCE_ONEC_IP)
        self.assertEqual(link.person.client_type, "private")
        self.assertEqual(link.person.name, "Иванов Иван Иванович")
        self.assertTrue(
            ClientContact.objects.filter(
                client=link.person,
                kind=ClientContact.KIND_PHONE,
                match_value="79130000001",
            ).exists()
        )

    def test_ready_ip_with_ambiguous_phone_is_sent_to_review_without_partial_import(self):
        for suffix in ("A", "B"):
            person = Client.objects.create(
                organization=self.organization,
                client_type="private",
                name=f"Иванов Иван {suffix}",
                phone="+7 913 000-00-01",
            )
            ClientContact.objects.create(
                client=person,
                kind=ClientContact.KIND_PHONE,
                value="+7 913 000-00-01",
                match_value="79130000001",
            )
        candidate = self.candidate()

        result = apply_ready_candidates(self.organization)

        candidate.refresh_from_db()
        self.assertEqual(result, {"imported": 0, "failed": 1})
        self.assertEqual(candidate.status, ClientImportCandidate.STATUS_REVIEW)
        self.assertFalse(
            ClientCRMProfile.objects.filter(onec_ref=candidate.source_ref).exists()
        )

    @override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID="")
    def test_scan_marks_retail_customer_invalid_and_is_idempotent_by_ref(self):
        with override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.id)):
            buyer_rows = [
                {
                    "Ref_Key": "22222222-2222-2222-2222-222222222222",
                    "Code": "000000001",
                    "Description": "Розничный покупатель",
                    "НаименованиеПолное": "Розничный покупатель",
                    "ЮридическоеФизическоеЛицо": "ФизическоеЛицо",
                    "ВидКонтрагента": "ФизическоеЛицо",
                    "ИНН": "",
                    "КПП": "",
                    "ФИО": "",
                    "ДатаРождения": "0001-01-01T00:00:00",
                    "НомерТелефонаДляПоиска": "",
                    "АдресЭПДляПоиска": "",
                    "Покупатель": True,
                    "Недействителен": False,
                }
            ]

            def fake_query(_config, entity_set, **kwargs):
                if entity_set == "Catalog_Контрагенты":
                    return {"rows": buyer_rows, "complete": True}
                return {"rows": [], "complete": True}

            with patch("pool_service.client_crm_import.config_from_settings", return_value=object()), patch(
                "pool_service.client_crm_import.fetch_metadata", return_value=b"metadata"
            ) as metadata_mock, patch(
                "pool_service.client_crm_import.query_1c_rows", side_effect=fake_query
            ):
                scan_onec_clients()
                scan_onec_clients()

        self.assertEqual(metadata_mock.call_count, 2)

        self.assertEqual(ClientImportCandidate.objects.count(), 1)
        candidate = ClientImportCandidate.objects.get()
        self.assertEqual(candidate.status, ClientImportCandidate.STATUS_INVALID)
        self.assertEqual(candidate.reason, "Системная карточка 1С")

    def test_existing_company_is_matched_by_inn_not_duplicated(self):
        company = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="ООО Альфа",
            company_name="ООО Альфа",
            inn="2222000000",
        )
        candidate = self.candidate(
            source_ref="33333333-3333-3333-3333-333333333333",
            source_kind=ClientImportCandidate.KIND_LEGAL,
            name="АЛЬФА ООО",
            legal_name="ООО АЛЬФА",
            inn="2222000000",
            phone="",
            email="",
            payload={"phones": [], "emails": [], "fio": ""},
            matched_client=company,
        )

        imported = apply_candidate(candidate)

        self.assertEqual(imported.pk, company.pk)
        self.assertEqual(Client.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(imported.crm_profile.onec_ref, candidate.source_ref)


    @override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID="")
    def test_request_scan_returns_immediately_and_reuses_active_run(self):
        user = get_user_model().objects.create_user(username="crm-import-owner")
        with override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.id)), patch(
            "pool_service.client_crm_import.start_client_import_worker", return_value=True
        ) as launcher:
            first, started_first = request_client_import_scan(user)
            second, started_second = request_client_import_scan(user)

        self.assertTrue(started_first)
        self.assertFalse(started_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.status, ClientImportRun.STATUS_PENDING)
        self.assertEqual(ClientImportRun.objects.count(), 1)
        launcher.assert_called_once_with(first.pk)


    def test_manual_resolution_changes_effective_type_and_allows_import(self):
        user = get_user_model().objects.create_user(username="resolver")
        candidate = self.candidate(
            source_ref="44444444-4444-4444-4444-444444444444",
            source_kind=ClientImportCandidate.KIND_LEGAL,
            name="Андреев Иван",
            legal_name="",
            inn="",
            status=ClientImportCandidate.STATUS_REVIEW,
            payload={
                "fio": "Андреев Иван",
                "phones": [{"value": "+7 913 237-27-27", "match": "79132372727", "label": "Основной"}],
                "emails": [],
            },
        )

        resolved = resolve_import_candidate(
            candidate.pk,
            ClientImportCandidate.RESOLUTION_PRIVATE,
            user,
        )
        self.assertEqual(resolved.status, ClientImportCandidate.STATUS_READY)
        self.assertEqual(resolved.effective_kind, ClientImportCandidate.KIND_PRIVATE)

        client = apply_candidate(resolved)
        self.assertEqual(client.client_type, "private")
        self.assertEqual(client.crm_profile.onec_ref, candidate.source_ref)

    def test_skip_resolution_is_not_mass_imported(self):
        user = get_user_model().objects.create_user(username="skip-resolver")
        candidate = self.candidate(
            source_ref="55555555-5555-5555-5555-555555555555",
            name="Прочие",
        )
        resolve_import_candidate(
            candidate.pk,
            ClientImportCandidate.RESOLUTION_SKIP,
            user,
        )

        result = apply_ready_candidates(self.organization)

        candidate.refresh_from_db()
        self.assertEqual(result["imported"], 0)
        self.assertEqual(candidate.status, ClientImportCandidate.STATUS_SKIPPED)
        self.assertIsNone(candidate.applied_at)

    @override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID="")
    def test_legal_phone_does_not_match_existing_private_client(self):
        private = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Иван Иванов",
            phone="+7 999 111-22-33",
        )
        buyer_rows = [
            {
                "Ref_Key": "66666666-6666-6666-6666-666666666666",
                "Code": "НФ-999999",
                "Description": "ООО Тест без ИНН",
                "НаименованиеПолное": "ООО Тест без ИНН",
                "ЮридическоеФизическоеЛицо": "ЮридическоеЛицо",
                "ВидКонтрагента": "ЮридическоеЛицо",
                "ИНН": "",
                "КПП": "",
                "ФИО": "",
                "ДатаРождения": "0001-01-01T00:00:00",
                "НомерТелефонаДляПоиска": "89991112233",
                "АдресЭПДляПоиска": "",
                "Покупатель": True,
                "Недействителен": False,
            }
        ]

        def fake_query(_config, entity_set, **kwargs):
            if entity_set == "Catalog_Контрагенты":
                return {"rows": buyer_rows, "complete": True}
            return {"rows": [], "complete": True}

        with override_settings(ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.id)), patch(
            "pool_service.client_crm_import.config_from_settings", return_value=object()
        ), patch(
            "pool_service.client_crm_import.fetch_metadata", return_value=b"metadata"
        ), patch(
            "pool_service.client_crm_import.query_1c_rows", side_effect=fake_query
        ):
            scan_onec_clients()

        candidate = ClientImportCandidate.objects.get(source_ref=buyer_rows[0]["Ref_Key"])
        self.assertEqual(candidate.status, ClientImportCandidate.STATUS_REVIEW)
        self.assertIsNone(candidate.matched_client)
        self.assertTrue(Client.objects.filter(pk=private.pk).exists())


    def test_duplicate_cannot_be_bypassed_with_type_resolution(self):
        user = get_user_model().objects.create_user(username="duplicate-resolver")
        candidate = self.candidate(
            source_ref="77777777-7777-7777-7777-777777777778",
            status=ClientImportCandidate.STATUS_DUPLICATE,
            reason="Несколько карточек Service2 совпали по: телефон",
        )

        with self.assertRaisesMessage(ValueError, "сначала нужно выбрать существующую карточку"):
            resolve_import_candidate(
                candidate.pk,
                ClientImportCandidate.RESOLUTION_LEGAL,
                user,
            )

        candidate.refresh_from_db()
        self.assertEqual(candidate.status, ClientImportCandidate.STATUS_DUPLICATE)
        self.assertEqual(candidate.resolution, ClientImportCandidate.RESOLUTION_AUTO)
