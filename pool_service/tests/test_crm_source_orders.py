"""Order-source references are not payments, revenue, or automatic attribution."""
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
import json
import uuid
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser, User
from django.core.exceptions import PermissionDenied
from django.test import TestCase
from django.utils import timezone

from pool_service import crm_source_orders as orders
from pool_service.client_crm_models import ClientCRMProfile
from pool_service.models import Client, CrmItem, Organization, OrganizationAccess
from pool_service.money_models import OneCMoneyForecastRow, OneCMoneyForecastSnapshot


class ObservedOrderReferenceTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Observed order test organization")
        self.other_org = Organization.objects.create(name="Foreign order test organization")
        self.owner = User.objects.create_user("order-reference-owner")
        self.owner_access = OrganizationAccess.objects.create(
            organization=self.org, user=self.owner, role="owner")
        self.manager = User.objects.create_user("order-reference-manager")
        OrganizationAccess.objects.create(organization=self.org, user=self.manager, role="manager")
        self.foreign = User.objects.create_user("order-reference-foreign")
        OrganizationAccess.objects.create(organization=self.other_org, user=self.foreign, role="owner")
        self.party = uuid.UUID("22222222-2222-4222-8222-222222222222")
        self.guid = uuid.UUID("11111111-1111-4111-8111-111111111111")
        self.client_record = Client.objects.create(
            organization=self.org, name="Synthetic order client", phone="70000000001")
        self.profile = ClientCRMProfile.objects.create(
            client=self.client_record, onec_ref=str(self.party),
            source=ClientCRMProfile.SOURCE_ONEC,
            last_synced_at=timezone.now()-timedelta(hours=1))
        self.item = CrmItem.objects.create(
            organization=self.org, client=self.client_record,
            direction=CrmItem.DIRECTION_SERVICE, title="Synthetic service request",
            created_by=self.owner)
        self.snapshot = self.new_snapshot()
        self.row = self.new_row()

    def new_snapshot(self, *, organization=None, active=True, minutes_ago=10):
        stamp = timezone.now()-timedelta(minutes=minutes_ago)
        return OneCMoneyForecastSnapshot.objects.create(
            organization=organization or self.org, source_at=stamp, fetched_at=stamp,
            activated_at=stamp, is_active=active, diagnostics={})

    def new_row(self, **changes):
        values = {
            "snapshot": self.snapshot, "source_identity": "synthetic:"+uuid.uuid4().hex,
            "direction": "receipt", "item_kind": "order_due", "order_guid": self.guid,
            "counterparty_guid": self.party, "order_number": "SYNTHETIC-1",
            "order_date": date(2026, 10, 20), "order_state": "В работе",
            "expected_amount": Decimal("917263.49"),
            "matched_paid_amount": Decimal("815142.37"),
            "remaining_amount": Decimal("102121.12"),
            "payment_match_status": "order_receivable_settled",
            "confirmation_status": "confirmed",
            "counterparty_name": "PRIVATE-SOURCE-NAME",
            "responsible_name": "PRIVATE-RESPONSIBLE",
            "source_payload": {"raw_secret": "PRIVATE-PAYLOAD"},
        }
        values.update(changes)
        return OneCMoneyForecastRow.objects.create(**values)

    def observed(self):
        return orders.observed(self.owner, self.org, self.item, str(self.guid))

    def assert_no_money_or_private_payload(self, value):
        serialized = json.dumps(value, default=str)
        for forbidden in (
                "expected_amount", "matched_paid_amount", "remaining_amount",
                "source_payload", "counterparty_name", "responsible_name",
                "917263.49", "815142.37", "102121.12",
                "PRIVATE-SOURCE-NAME", "PRIVATE-RESPONSIBLE", "PRIVATE-PAYLOAD"):
            self.assertNotIn(forbidden, serialized)

    def test_exact_source_identity_returns_only_observed_business_reference(self):
        result = self.observed()
        self.assertEqual(result["guid"], self.guid)
        self.assertEqual(result["number"], "SYNTHETIC-1")
        self.assertEqual(result["date"], "2026-10-20")
        evidence = result["evidence"]
        self.assertEqual(evidence["source"], "active_money_forecast_planning_snapshot")
        self.assertEqual(evidence["snapshot_id"], self.snapshot.pk)
        self.assertEqual(evidence["profile_id"], self.profile.pk)
        self.assertIs(evidence["manual_confirmation"], True)
        self.assertIs(evidence["payment_confirmed"], False)
        self.assertEqual(len(evidence["business_sha256"]), 64)
        self.assertEqual(len(evidence["order_guid_sha256"]), 64)
        self.assert_no_money_or_private_payload(result)

    def test_permissions_are_scoped_and_restricted_results_do_not_leak_the_reference(self):
        self.assertTrue(orders.allowed(self.owner, self.org))
        link = SimpleNamespace(order_guid=self.guid, order_evidence=self.observed()["evidence"])
        for actor in (self.manager, self.foreign, AnonymousUser()):
            with self.subTest(actor=actor):
                self.assertFalse(orders.allowed(actor, self.org))
                self.assertIsNone(orders.candidates(actor, self.org, self.item))
                with self.assertRaises(PermissionDenied):
                    orders.observed(actor, self.org, self.item, self.guid)
                projected = orders.state(actor, self.org, link, self.item)
                self.assertEqual(projected["state"], "restricted")
                self.assertEqual(set(projected), {"state", "label"})
                self.assertNotIn(str(self.guid), str(projected))
                self.assert_no_money_or_private_payload(projected)

    def test_role_revocation_and_inactive_actor_are_checked_again(self):
        self.assertTrue(orders.allowed(self.owner, self.org))
        self.owner_access.role = "manager"
        self.owner_access.save(update_fields=["role"])
        self.assertFalse(orders.allowed(self.owner, self.org))
        with self.assertRaises(PermissionDenied):
            self.observed()
        self.owner_access.role = "owner"
        self.owner_access.save(update_fields=["role"])
        self.owner.is_active = False
        self.owner.save(update_fields=["is_active"])
        self.assertFalse(orders.allowed(self.owner, self.org))

    def test_foreign_item_or_client_is_rejected_even_with_matching_display_values(self):
        foreign_client = Client.objects.create(
            organization=self.other_org, name=self.client_record.name,
            phone=self.client_record.phone)
        foreign_item = CrmItem.objects.create(
            organization=self.other_org, client=foreign_client,
            direction="service", title=self.item.title)
        self.assertIsNone(orders.observed(self.owner, self.org, foreign_item, self.guid))
        self.item.client = foreign_client
        self.item.save(update_fields=["client"])
        self.assertIsNone(self.observed())
        self.assertEqual(orders.candidates(self.owner, self.org, self.item), [])

    def test_profile_must_be_synced_unmerged_onec_identity_with_nonzero_uuid(self):
        cases = (
            {"source": "manual"}, {"last_synced_at": None},
            {"onec_ref": "not-a-uuid"}, {"onec_ref": str(uuid.UUID(int=0))},
            {"merged_into_id": self.client_record.pk},
        )
        original = {"source": "onec", "last_synced_at": self.profile.last_synced_at,
                    "onec_ref": str(self.party), "merged_into_id": None}
        for change in cases:
            with self.subTest(change=change):
                ClientCRMProfile.objects.filter(pk=self.profile.pk).update(**change)
                self.assertIsNone(self.observed())
                ClientCRMProfile.objects.filter(pk=self.profile.pk).update(**original)

    def test_exact_uuid_party_and_order_are_required_name_or_phone_never_suffices(self):
        self.row.counterparty_guid = uuid.UUID("33333333-3333-4333-8333-333333333333")
        self.row.save(update_fields=["counterparty_guid"])
        self.assertIsNone(self.observed())
        self.assertEqual(orders.candidates(self.owner, self.org, self.item), [])
        for raw in ("not-a-uuid", uuid.UUID(int=0), None):
            with self.subTest(guid=raw):
                self.assertIsNone(orders.observed(self.owner, self.org, self.item, raw))

    def test_installments_deduplicate_and_money_changes_do_not_change_business_evidence(self):
        before = self.observed()
        self.new_row(item_kind="order_schedule", expected_amount=Decimal("1.00"),
                     matched_paid_amount=None, remaining_amount=None)
        candidates = orders.candidates(self.owner, self.org, self.item)
        self.assertEqual(len(candidates), 1)
        after = self.observed()
        self.assertEqual(after["evidence"]["business_sha256"], before["evidence"]["business_sha256"])
        OneCMoneyForecastRow.objects.filter(snapshot=self.snapshot).update(
            expected_amount=Decimal("5.00"), matched_paid_amount=Decimal("5.00"),
            remaining_amount=Decimal("0.00"), confirmation_status="excluded")
        self.assertEqual(self.observed()["evidence"]["business_sha256"],
                         before["evidence"]["business_sha256"])
        self.assert_no_money_or_private_payload(candidates)

    def test_conflicting_installment_business_data_is_ambiguous(self):
        cases = (
            {"counterparty_guid": uuid.UUID("44444444-4444-4444-8444-444444444444")},
            {"order_number": "CONFLICT"}, {"order_date": date(2026, 10, 19)},
            {"order_state": "Не обработан"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                row = self.new_row(**changes)
                self.assertIsNone(self.observed())
                self.assertEqual(orders.candidates(self.owner, self.org, self.item), [])
                row.delete()

    def test_payment_direction_supplier_or_inactive_source_is_not_a_customer_order(self):
        self.row.direction = "payment"
        self.row.save(update_fields=["direction"])
        self.assertIsNone(self.observed())
        self.row.direction, self.row.item_kind = "receipt", "supplier_schedule"
        self.row.save(update_fields=["direction", "item_kind"])
        self.assertIsNone(self.observed())
        self.row.item_kind = "order_due"
        self.row.save(update_fields=["item_kind"])
        self.snapshot.is_active = False
        self.snapshot.save(update_fields=["is_active"])
        self.assertIsNone(self.observed())

    def test_new_active_snapshot_without_order_does_not_reuse_old_historical_rows(self):
        old = self.observed()
        self.snapshot.is_active = False
        self.snapshot.save(update_fields=["is_active"])
        self.new_snapshot(minutes_ago=0)
        self.assertIsNone(self.observed())
        projected = orders.state(self.owner, self.org,
            SimpleNamespace(order_guid=self.guid, order_evidence=old["evidence"]), self.item)
        self.assertEqual(projected["state"], "unknown")
        self.assertEqual(set(projected), {"state", "label"})

    def test_active_snapshot_rotation_during_resolution_discards_the_result(self):
        replacement = self.new_snapshot(minutes_ago=0)
        with patch("pool_service.crm_source_orders._active_snapshot",
                   side_effect=[self.snapshot, replacement]):
            self.assertIsNone(self.observed())

    def test_more_than_one_hundred_installments_never_claims_complete_reference(self):
        OneCMoneyForecastRow.objects.bulk_create([
            OneCMoneyForecastRow(
                snapshot=self.snapshot, source_identity="bounded:"+str(index),
                direction="receipt", item_kind="order_schedule", order_guid=self.guid,
                counterparty_guid=self.party, order_number=self.row.order_number,
                order_date=self.row.order_date, order_state=self.row.order_state)
            for index in range(100)])
        self.assertIsNone(self.observed())

    def test_stored_evidence_is_revalidated_and_unsupported_or_changed_sources_stay_unknown(self):
        result = self.observed()
        link = SimpleNamespace(order_guid=self.guid, order_evidence=result["evidence"])
        projected = orders.state(self.owner, self.org, link, self.item)
        self.assertEqual(projected["state"], "observed")
        self.assertIs(projected["payment_confirmed"], False)
        self.assert_no_money_or_private_payload(projected)
        for stored in ({}, {**result["evidence"], "source": "manual_guess"},
                       {**result["evidence"], "business_sha256": "0"*64}):
            with self.subTest(stored=stored):
                link.order_evidence = stored
                unknown = orders.state(self.owner, self.org, link, self.item)
                self.assertEqual(unknown["state"], "unknown")
                self.assertNotIn("guid", unknown)
        link.order_evidence = result["evidence"]
        self.row.order_state = "Завершен"
        self.row.save(update_fields=["order_state"])
        self.assertEqual(orders.state(self.owner, self.org, link, self.item)["state"], "unknown")

    def test_unknown_reference_does_not_turn_into_zero_payments_or_revenue(self):
        for link in (None, SimpleNamespace(order_guid=None, order_evidence={})):
            projected = orders.state(self.owner, self.org, link, self.item)
            self.assertEqual(projected["state"], "unknown")
            self.assertEqual(set(projected), {"state", "label"})
            self.assert_no_money_or_private_payload(projected)
