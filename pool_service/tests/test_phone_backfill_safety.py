from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.db.models.query import QuerySet
from django.test import SimpleTestCase, TransactionTestCase
from django.utils import timezone

from pool_service.client_crm_models import ClientContact
from pool_service.client_phone_matching import clients_by_phone
from pool_service.communication_models import PhoneCall, TelephonyConnection
from pool_service.models import Client, Organization
from pool_service.phone_utils import canonical_phone_value, normalize_phone


class InternationalPhoneKeyTests(SimpleTestCase):
    def test_country_code_variants_share_key_without_changing_display(self):
        for key, variants in (
            ("+358401234567", ("+358 40 123 4567", "358401234567")),
            ("+442079460958", ("+44 20 7946 0958", "442079460958")),
        ):
            for value in variants:
                with self.subTest(value=value):
                    self.assertEqual(normalize_phone(value), key)
                    self.assertEqual(normalize_phone(key), key)
                    self.assertEqual(canonical_phone_value(value), value)

    def test_explicit_foreign_ten_digits_never_become_russian(self):
        self.assertEqual(normalize_phone("+3512345678"), "+3512345678")
        self.assertEqual(canonical_phone_value("+3512345678"), "+3512345678")
        self.assertNotEqual(
            normalize_phone("+3512345678"), normalize_phone("3512345678")
        )


class PhoneBackfillSafetyTests(TransactionTestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Backfill test org")
        self.customer = Client.objects.create(
            organization=self.organization,
            client_type="private",
            name="Synthetic customer",
            phone="89621112233",
        )
        self.contact = ClientContact.objects.create(
            client=self.customer,
            kind=ClientContact.KIND_PHONE,
            value="89621112233",
            match_value="79621112233",
            sources=["onec"],
        )
        self.telephony = TelephonyConnection.objects.create(
            organization=self.organization,
            name="Synthetic telephony",
            external_id="backfill-safety",
        )
        self.call = PhoneCall.objects.create(
            organization=self.organization,
            connection=self.telephony,
            external_id="backfill-test-call",
            phone_number="79621112233",
            direction=PhoneCall.DIRECTION_IN,
            started_at=timezone.now(),
            result=PhoneCall.RESULT_ANSWERED,
        )

    def _run_and_observe_initial_reads(self, *, apply):
        observed = {}
        fetch_all = QuerySet._fetch_all
        watched = {Client, ClientContact, PhoneCall}

        def observe(queryset):
            if queryset.model in watched and queryset.model not in observed:
                observed[queryset.model] = (
                    connection.in_atomic_block,
                    queryset.query.select_for_update,
                )
            return fetch_all(queryset)

        # TransactionTestCase intentionally has no enclosing test transaction.
        self.assertFalse(connection.in_atomic_block)
        with patch.object(QuerySet, "_fetch_all", new=observe):
            call_command("normalize_client_phones", apply=apply, stdout=StringIO())
        return observed

    def test_apply_locks_every_initial_snapshot_inside_write_transaction(self):
        observed = self._run_and_observe_initial_reads(apply=True)
        self.assertEqual(set(observed), {Client, ClientContact, PhoneCall})
        self.assertTrue(all(value == (True, True) for value in observed.values()))
        self.customer.refresh_from_db()
        self.call.refresh_from_db()
        self.assertEqual(self.customer.phone, "+7 962 111 2233")
        self.assertEqual(self.call.client_id, self.customer.pk)

    def test_dry_run_neither_locks_nor_writes(self):
        statements = []

        def record(execute, sql, params, many, context):
            statements.append(sql.lstrip().split(None, 1)[0].upper())
            return execute(sql, params, many, context)

        with connection.execute_wrapper(record):
            observed = self._run_and_observe_initial_reads(apply=False)
        self.assertTrue(all(value == (False, False) for value in observed.values()))
        self.assertFalse({"INSERT", "UPDATE", "DELETE"}.intersection(statements))
        self.customer.refresh_from_db()
        self.contact.refresh_from_db()
        self.assertEqual(self.customer.phone, "89621112233")
        self.assertEqual(self.contact.match_value, "79621112233")

    def test_apply_re_reads_changes_made_after_separate_dry_run(self):
        call_command("normalize_client_phones", stdout=StringIO())
        Client.objects.filter(pk=self.customer.pk).update(phone="89622223344")
        ClientContact.objects.filter(pk=self.contact.pk).update(
            value="89622223344", match_value="79622223344"
        )
        PhoneCall.objects.filter(pk=self.call.pk).update(
            client=self.customer, contact_name="Manually selected"
        )
        call_command("normalize_client_phones", apply=True, stdout=StringIO())
        self.customer.refresh_from_db()
        self.contact.refresh_from_db()
        self.call.refresh_from_db()
        self.assertEqual(self.customer.phone, "+7 962 222 3344")
        self.assertEqual(self.contact.match_value, "9622223344")
        self.assertEqual(self.call.contact_name, "Manually selected")
        self.assertEqual(self.call.client_id, self.customer.pk)

    def test_failure_rolls_back_client_contact_and_call_changes(self):
        with patch.object(ClientContact, "save", side_effect=RuntimeError("test failure")):
            with self.assertRaisesMessage(RuntimeError, "test failure"):
                call_command("normalize_client_phones", apply=True, stdout=StringIO())
        self.customer.refresh_from_db()
        self.contact.refresh_from_db()
        self.call.refresh_from_db()
        self.assertEqual(self.customer.phone, "89621112233")
        self.assertEqual(self.contact.value, "89621112233")
        self.assertEqual(self.contact.match_value, "79621112233")
        self.assertIsNone(self.call.client_id)
        self.assertEqual(self.call.phone_number, "79621112233")

    def test_foreign_contact_only_client_matches_both_call_representations(self):
        Client.objects.filter(pk=self.customer.pk).update(phone="")
        ClientContact.objects.filter(pk=self.contact.pk).update(
            value="+358 40 123 4567", match_value="358401234567"
        )
        PhoneCall.objects.filter(pk=self.call.pk).update(phone_number="358401234567")
        call_command("normalize_client_phones", apply=True, stdout=StringIO())
        self.contact.refresh_from_db()
        self.call.refresh_from_db()
        self.assertEqual(self.contact.value, "+358 40 123 4567")
        self.assertEqual(self.contact.match_value, "+358401234567")
        self.assertEqual(self.call.phone_number, "358401234567")
        self.assertEqual(self.call.client_id, self.customer.pk)
        for value in ("+358401234567", "358401234567"):
            self.assertEqual(
                [item.pk for item in clients_by_phone(self.organization, value)],
                [self.customer.pk],
            )

    def test_second_apply_is_no_op_for_persisted_data(self):
        call_command("normalize_client_phones", apply=True, stdout=StringIO())

        def snapshot():
            return (
                list(Client.objects.order_by("pk").values()),
                list(ClientContact.objects.order_by("pk").values()),
                list(PhoneCall.objects.order_by("pk").values()),
            )

        before = snapshot()
        call_command("normalize_client_phones", apply=True, stdout=StringIO())
        self.assertEqual(snapshot(), before)
