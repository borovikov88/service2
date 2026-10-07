"""Prepared CRM helpers; URL/page integration is not asserted by these tests."""
from datetime import timedelta
from pathlib import Path
import shutil
import subprocess

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, SimpleTestCase, TestCase
from django.utils import timezone

from pool_service.client_crm_models import ClientContact
from pool_service.communication_crm import client_lookup, resolved_calls_for_client
from pool_service.communication_models import PhoneCall, TelephonyConnection
from pool_service.models import Client, Organization, OrganizationAccess


class OptionalPickerScriptTests(SimpleTestCase):
    def test_optional_filter_behaviour(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js unavailable")
        result = subprocess.run(
            [node, "--test", str(Path(__file__).with_name("crm_picker_optional.test.cjs"))],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class PreparedCallCRMHelperTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Prepared CRM tests", paid_until=timezone.now() + timedelta(days=30))
        self.foreign = Organization.objects.create(name="Other prepared CRM tests")
        self.owner = get_user_model().objects.create_user(username="prepared-crm-owner")
        OrganizationAccess.objects.create(user=self.owner, organization=self.org, role="owner")
        self.connection = TelephonyConnection.objects.create(organization=self.org, name="Test line")
        self.factory = RequestFactory()

    def customer(self, phone="", **kwargs):
        return Client.objects.create(
            organization=kwargs.pop("organization", self.org),
            client_type="private", name=kwargs.pop("name", "Example customer"), phone=phone, **kwargs,
        )

    def call(self, phone="+7 900 123 4567", **kwargs):
        return PhoneCall.objects.create(
            organization=self.org, connection=self.connection, external_id=str(PhoneCall.objects.count()),
            phone_number=phone, source_kind="telephony", direction="in", result="answered",
            started_at=timezone.now() - timedelta(days=7), **kwargs,
        )

    def ids(self, customer):
        return list(resolved_calls_for_client(PhoneCall.objects.all(), customer).values_list("pk", flat=True))

    def lookup(self, query, user=None):
        request = self.factory.get("/unused-test-path/", {"q": query})
        request.user = user or self.owner
        return client_lookup(request, "telephony")

    def test_historical_primary_phone_resolves_without_a_database_update(self):
        call = self.call()
        customer = self.customer(phone="89001234567")
        self.assertEqual(self.ids(customer), [call.pk])
        call.refresh_from_db()
        self.assertIsNone(call.client_id)

    def test_additional_contact_resolves_history(self):
        call = self.call()
        customer = self.customer()
        ClientContact.objects.create(client=customer, kind="phone", value="89001234567", match_value="9001234567")
        self.assertEqual(self.ids(customer), [call.pk])

    def test_manual_assignment_wins(self):
        by_phone = self.customer(phone="89001234567")
        explicit = self.customer(name="Explicit assignment")
        call = self.call(client=explicit)
        self.assertEqual(self.ids(by_phone), [])
        self.assertEqual(self.ids(explicit), [call.pk])
        call.refresh_from_db()
        self.assertEqual(call.client_id, explicit.pk)

    def test_shared_phones_do_not_choose_arbitrarily(self):
        first = self.customer(phone="89001234567")
        second = self.customer(phone="+7 900 123 4567")
        self.call()
        self.assertEqual(self.ids(first), [])
        self.assertEqual(self.ids(second), [])

    def test_foreign_suffix_and_tenant_are_not_phone_identity(self):
        russian = self.customer(phone="+7 900 123 4567")
        international = self.customer(phone="+34 9001234567")
        foreign_tenant = self.customer(phone="+7 900 123 4567", organization=self.foreign)
        first = self.call()
        second = self.call(phone="+34 9001234567")
        self.assertEqual(self.ids(russian), [first.pk])
        self.assertEqual(self.ids(international), [second.pk])
        self.assertEqual(self.ids(foreign_tenant), [])

    def test_lookup_has_no_initial_results_and_bounds_matches(self):
        import json
        for index in range(25):
            self.customer(name=f"Needle {index:02d}")
        self.customer(name="Needle foreign", organization=self.foreign)
        for query in ("", "Ne", " Ne - "):
            response = self.lookup(query)
            self.assertEqual(json.loads(response.content), {"results": [], "has_more": False})
            self.assertEqual(response["Cache-Control"], "private, no-store")
        data = json.loads(self.lookup("Needle").content)
        self.assertEqual(len(data["results"]), 20)
        self.assertTrue(data["has_more"])
        self.assertNotIn("Needle foreign", [item["name"] for item in data["results"]])

    def test_lookup_preserves_hyphen_and_phone_matching(self):
        import json
        customer = self.customer(name="Строй-инвест Семён", phone="+7 900 123 4567")
        for query in ("Строй инвест", "СЕМЕН", "89001234567", "79001234567"):
            with self.subTest(query=query):
                data = json.loads(self.lookup(query).content)
                self.assertEqual([item["id"] for item in data["results"]], [customer.pk])

    def test_lookup_requires_communication_permission(self):
        accountant = get_user_model().objects.create_user(username="prepared-crm-accountant")
        OrganizationAccess.objects.create(user=accountant, organization=self.org, role="accountant")
        with self.assertRaises(PermissionDenied):
            self.lookup("Example", user=accountant)
