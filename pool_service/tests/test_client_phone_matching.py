from types import SimpleNamespace

from django.test import TestCase

from pool_service.client_crm_models import ClientCompanyLink, ClientContact
from pool_service.communication_api import _megafon_contact
from pool_service.communication_views import _prepare_call_display
from pool_service.models import Client, Organization
from pool_service.phone_utils import format_phone, normalize_phone


class ClientPhoneFormattingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Phone test")

    def _client(self, *, name, client_type="private", phone=None):
        return Client.objects.create(
            organization=self.organization,
            client_type=client_type,
            name=name,
            company_name=name if client_type == "legal" else None,
            phone=phone,
        )

    def test_russian_phone_normalization_and_display(self):
        self.assertEqual(normalize_phone("8 (923) 163-70-95"), "79231637095")
        self.assertEqual(normalize_phone("9231637095"), "79231637095")
        self.assertEqual(format_phone("8 (923) 163-70-95"), "+7 923 163 7095")
        self.assertEqual(format_phone("9231637095"), "+7 923 163 7095")

    def test_megafon_contact_matches_secondary_crm_phone(self):
        client = self._client(name="Дарья", phone="+7 999 000 0000")
        ClientContact.objects.create(
            client=client,
            kind=ClientContact.KIND_PHONE,
            value="+7 923 163 7095",
            match_value="79231637095",
        )

        matched = _megafon_contact(self.organization, "8 923 163-70-95")

        self.assertEqual(matched.pk, client.pk)

    def test_megafon_contact_does_not_guess_between_unrelated_clients(self):
        first = self._client(name="Первый")
        second = self._client(name="Второй")
        for client in (first, second):
            ClientContact.objects.create(
                client=client,
                kind=ClientContact.KIND_PHONE,
                value="+7 923 163 7095",
                match_value="79231637095",
            )

        self.assertIsNone(_megafon_contact(self.organization, "+7 923 163 7095"))

    def test_linked_ip_company_and_person_resolve_to_person(self):
        company = self._client(
            name="ИП Иванов Иван Иванович",
            client_type="legal",
            phone="+7 923 163 7095",
        )
        person = self._client(
            name="Иванов Иван Иванович",
            phone="+7 923 163 7095",
        )
        ClientCompanyLink.objects.create(
            company=company,
            person=person,
            source=ClientCompanyLink.SOURCE_ONEC_IP,
            automatic=True,
        )

        matched = _megafon_contact(self.organization, "89231637095")

        self.assertEqual(matched.pk, person.pk)

    def test_call_list_formats_number_and_resolves_secondary_phone(self):
        client = self._client(name="Клиент")
        ClientContact.objects.create(
            client=client,
            kind=ClientContact.KIND_PHONE,
            value="+7 901 792 0288",
            match_value="79017920288",
        )
        call = SimpleNamespace(
            phone_number="8 (901) 792-02-88",
            client=None,
            client_id=None,
        )

        prepared = _prepare_call_display([call], self.organization)

        self.assertEqual(prepared[0].display_phone, "+7 901 792 0288")
        self.assertEqual(prepared[0].resolved_client.pk, client.pk)
