from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from pool_service.client_crm_import import apply_ready_candidates
from pool_service.client_crm_models import ClientCRMProfile, ClientCompanyLink, ClientImportCandidate
from pool_service.client_crm_sync import sync_recent_onec_clients
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
