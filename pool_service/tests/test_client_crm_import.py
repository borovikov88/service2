from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from pool_service.client_crm_import import apply_candidate, apply_ready_candidates, scan_onec_clients
from pool_service.client_crm_models import (
    ClientCRMProfile,
    ClientCompanyLink,
    ClientContact,
    ClientImportCandidate,
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
                "pool_service.client_crm_import.query_1c_rows", side_effect=fake_query
            ):
                scan_onec_clients()
                scan_onec_clients()

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
