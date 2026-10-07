from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse

from pool_service.client_crm_models import ClientCompanyLink, ClientContact
from pool_service.client_phone_matching import clients_by_phone, clients_by_phones
from pool_service.communication_models import PhoneCall, TelephonyConnection
from pool_service.forms import ClientCreateForm
from pool_service.models import Client, Organization, OrganizationAccess
from pool_service.phone_utils import canonical_phone_value, format_phone, normalize_account_phone, normalize_phone


class PhoneUtilityTests(TestCase):
    def test_russian_variants_share_one_match_value_and_display(self):
        for value in ("89628111913", "79628111913", "9628111913", "+7 962 811 1913"):
            self.assertEqual(normalize_phone(value), "9628111913")
            self.assertEqual(format_phone(value), "+7 962 811 1913")
            self.assertEqual(canonical_phone_value(value), "+7 962 811 1913")

    def test_account_phone_normalization_remains_strict_russian(self):
        self.assertEqual(normalize_account_phone("+7 962 811 1913"), "9628111913")
        self.assertEqual(normalize_account_phone("89628111913"), "9628111913")
        self.assertEqual(normalize_account_phone("+7 123 456 7"), "")
        self.assertEqual(normalize_account_phone("+358 40 123 4567"), "")

    def test_explicit_foreign_number_is_not_rewritten_as_russian(self):
        self.assertEqual(normalize_phone("+358 40 123 4567"), "+358401234567")
        self.assertEqual(format_phone("+358 40 123 4567"), "+358 40 123 4567")
        self.assertEqual(
            canonical_phone_value("+358 40 123 4567"),
            "+358 40 123 4567",
        )


class ClientPhoneIntegrationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Phone CRM org")
        self.owner = get_user_model().objects.create_user(
            username="phone-owner",
            password="test-pass",
        )
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.organization,
            role="owner",
        )

    def test_manual_client_form_persists_canonical_phone(self):
        form = ClientCreateForm(
            {
                "client_type": "private",
                "first_name": "Иван",
                "last_name": "Иванов",
                "phone": "89628111913",
                "email": "",
                "company_name": "",
                "inn": "",
                "contact_position": "",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        client = form.save()
        self.assertEqual(client.phone, "+7 962 811 1913")
        contact = ClientContact.objects.get(
            client=client,
            kind=ClientContact.KIND_PHONE,
        )
        self.assertEqual(contact.value, "+7 962 811 1913")
        self.assertEqual(contact.match_value, "9628111913")
        self.assertTrue(contact.is_primary)
        self.assertIn("manual", contact.sources)

    def test_phone_audit_does_not_print_customer_phone_values(self):
        Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Private audit client",
            phone="89628111913",
        )
        output = StringIO()
        call_command("normalize_client_phones", stdout=output)
        rendered = output.getvalue()
        self.assertIn("Phone audit:", rendered)
        self.assertIn("Phone conflict summary:", rendered)
        self.assertNotIn("9628111913", rendered)
        self.assertNotIn("89628111913", rendered)

    def test_phone_resolver_uses_client_contact_when_legacy_phone_is_empty(self):
        company = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name='ООО "Ромашка"',
            phone="",
        )
        ClientContact.objects.create(
            client=company,
            kind=ClientContact.KIND_PHONE,
            value="+7 962 811 1913",
            match_value="9628111913",
        )
        self.assertEqual(
            [item.pk for item in clients_by_phone(self.organization, "89628111913")],
            [company.pk],
        )

    def test_phone_resolver_returns_all_ambiguous_clients(self):
        first = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Первый",
            phone="+7 962 811 1913",
        )
        second = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="Второй",
            phone="79628111913",
        )
        matches = clients_by_phone(self.organization, "9628111913")
        self.assertEqual({item.pk for item in matches}, {first.pk, second.pk})

    def test_phone_resolver_batches_multiple_numbers(self):
        legacy_client = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Legacy",
            phone="89621112233",
        )
        contact_client = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name="Contact",
            phone="",
        )
        ClientContact.objects.create(
            client=contact_client,
            kind=ClientContact.KIND_PHONE,
            value="+7 905 083 0915",
            match_value="9050830915",
        )

        with self.assertNumQueries(3):
            matches = clients_by_phones(
                self.organization,
                ["+7 962 111 2233", "89050830915", "+7 962 111 2233"],
            )

        self.assertEqual(
            [item.pk for item in matches["9621112233"]],
            [legacy_client.pk],
        )
        self.assertEqual(
            [item.pk for item in matches["9050830915"]],
            [contact_client.pk],
        )

    def test_normalization_command_merges_duplicate_contacts_and_backfills_calls(self):
        client = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Клиент",
            phone="89628111913",
        )
        ClientContact.objects.create(
            client=client,
            kind=ClientContact.KIND_PHONE,
            value="89628111913",
            match_value="79628111913",
            is_primary=True,
            sources=["onec"],
        )
        ClientContact.objects.create(
            client=client,
            kind=ClientContact.KIND_PHONE,
            value="+7 (962) 811-19-13",
            match_value="9628111913",
            sources=["manual"],
        )
        telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="Test",
            external_id="test-phone-normalize",
        )
        call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="call-normalize",
            phone_number="79628111913",
            direction=PhoneCall.DIRECTION_IN,
            started_at="2026-10-06T00:00:00Z",
            result=PhoneCall.RESULT_ANSWERED,
        )
        explicitly_assigned = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Explicit client",
            phone="",
        )
        assigned_call = PhoneCall.objects.create(
            organization=self.organization,
            connection=telephony,
            external_id="call-explicit",
            client=explicitly_assigned,
            contact_name=explicitly_assigned.name,
            phone_number="79628111913",
            direction=PhoneCall.DIRECTION_IN,
            started_at="2026-10-06T00:01:00Z",
            result=PhoneCall.RESULT_ANSWERED,
        )

        output = StringIO()
        call_command("normalize_client_phones", "--apply", stdout=output)

        client.refresh_from_db()
        call.refresh_from_db()
        assigned_call.refresh_from_db()
        contacts = list(ClientContact.objects.filter(client=client))
        self.assertEqual(client.phone, "+7 962 811 1913")
        self.assertEqual(len(contacts), 1)
        self.assertEqual(contacts[0].value, "+7 962 811 1913")
        self.assertEqual(contacts[0].match_value, "9628111913")
        self.assertEqual(set(contacts[0].sources), {"onec", "manual"})
        self.assertEqual(call.phone_number, "+7 962 811 1913")
        self.assertEqual(call.client_id, client.pk)
        self.assertEqual(assigned_call.client_id, explicitly_assigned.pk)
        self.assertEqual(assigned_call.contact_name, explicitly_assigned.name)
        self.assertEqual(assigned_call.phone_number, "+7 962 811 1913")


class ClientRelationshipUiTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Relationship org")
        self.owner = get_user_model().objects.create_user(
            username="relationship-owner",
            password="test-pass",
        )
        OrganizationAccess.objects.create(
            user=self.owner,
            organization=self.organization,
            role="owner",
        )
        self.company = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name='ООО "Ромашка"',
            phone="89628111913",
            inn="2222000000",
        )
        self.person = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Иванов Иван Иванович",
            phone="79050830915",
        )
        self.client.force_login(self.owner)

    def test_owner_can_add_and_update_company_person_link(self):
        response = self.client.post(
            reverse("client_detail", args=[self.company.pk]),
            {
                "action": "add_company_link",
                "related_client": self.person.pk,
                "position": "Директор",
                "is_primary": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        link = ClientCompanyLink.objects.get(
            company=self.company,
            person=self.person,
        )
        self.assertEqual(link.position, "Директор")
        self.assertTrue(link.is_primary)

        page = self.client.get(reverse("client_detail", args=[self.company.pk]))
        self.assertContains(page, "Контактные лица")
        self.assertContains(page, "Иванов Иван Иванович")
        self.assertContains(page, "+7 905 083 0915")

    def test_client_list_keeps_legacy_company_contact_without_link(self):
        legacy_company = Client.objects.create(
            organization=self.organization,
            client_type="legal",
            name='ООО "Старый контакт"',
            company_name='ООО "Старый контакт"',
            first_name="Петр",
            last_name="Петров",
            contact_position="Директор",
            phone="89621112233",
        )
        page = self.client.get(reverse("clients_list"))
        self.assertContains(page, "Петр Петров")
        self.assertContains(page, "Директор")
        self.assertContains(page, "+7 962 111 2233")
        self.assertFalse(
            ClientCompanyLink.objects.filter(company=legacy_company).exists()
        )

    def test_client_list_contains_live_search_and_relationship_search_terms(self):
        ClientCompanyLink.objects.create(
            company=self.company,
            person=self.person,
            position="Директор",
        )
        page = self.client.get(reverse("clients_list"))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'data-client-search="companies"', html=False)
        self.assertContains(page, 'data-client-search="people"', html=False)
        self.assertContains(page, "Иванов Иван Иванович")
        self.assertContains(page, 'data-search=', html=False)
