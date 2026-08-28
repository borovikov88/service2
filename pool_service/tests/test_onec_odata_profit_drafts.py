from datetime import date, timedelta
from decimal import Decimal
import hashlib
import importlib
import inspect
import json
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.odata_profit import ODataConfig, ODataPreviewError
from pool_service.finance_forms import ODataProfitDraftForm
from pool_service.finance_imports.odata_profit_drafts import (
    ODataDraftError,
    confirm_odata_profit,
    create_odata_profit_draft,
)
from pool_service.finance_imports.monthly_profit_parser import classify_nomenclature_type
from pool_service.models import (
    OneCImportBatch,
    OneCMonthlyProfit,
    OneCReportPeriodActivation,
    OneCReportPeriodState,
    Organization,
    OrganizationAccess,
)
from pool_service.tests.test_onec_odata_profit_preview import FakeOpener


ORG = "11111111-1111-1111-1111-111111111111"
ITEM = "33333333-3333-3333-3333-333333333333"
CUSTOMER = "44444444-4444-4444-4444-444444444444"
RECORDER = "55555555-5555-5555-5555-555555555555"
RESPONSIBLE = "66666666-6666-6666-6666-666666666666"
BASE_URL = "https://fresh.example/odata/standard.odata/"


def config(**overrides):
    values = {
        "base_url": BASE_URL,
        "organization_guids": (ORG,),
        "timeout_seconds": 7,
        "max_pages": 10,
        "max_rows": 1000,
    }
    values.update(overrides)
    return ODataConfig(**values)


def profit_row(
    line=1, *, period="2026-05-15T10:00:00+03:00", organization=ORG,
    recorder=RECORDER, item=ITEM, customer=CUSTOMER, responsible=RESPONSIBLE,
    revenue="100.00", cost="40.00",
):
    return {
        "Recorder": recorder,
        "LineNumber": line,
        "Period": period,
        "Active": True,
        "Организация_Key": organization,
        "Номенклатура_Key": item,
        "Контрагент_Key": customer,
        "Ответственный_Key": responsible,
        "Документ": "Реализация 1",
        "Количество": "2",
        "Сумма": revenue,
        "СуммаНДС": "10.00",
        "Себестоимость": cost,
    }


def reference_payload(
    guid, description, *, article=None, deletion_mark=False,
    nomenclature_type=None,
):
    row = {
        "Ref_Key": guid,
        "Description": description,
        "DeletionMark": deletion_mark,
    }
    if article is not None:
        row["Артикул"] = article
        row["ТипНоменклатуры"] = nomenclature_type or "Запас"
    return {"value": [row]}


def successful_opener(rows):
    return FakeOpener(
        {"value": rows},
        reference_payload(ITEM, "Товар из 1С", article="A-1"),
        reference_payload(CUSTOMER, "Покупатель из 1С"),
        reference_payload(RESPONSIBLE, "Ответственный из 1С"),
    )


class ODataProfitDraftTests(TestCase):
    def setUp(self):
        self.private_dir = TemporaryDirectory()
        self.settings_override = override_settings(PRIVATE_MEDIA_ROOT=self.private_dir.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(self.private_dir.cleanup)
        self.organization = Organization.objects.create(
            name="OData draft", paid_until=timezone.now() + timedelta(days=30)
        )
        self.target_override = override_settings(
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.pk)
        )
        self.target_override.enable()
        self.addCleanup(self.target_override.disable)
        self.user = User.objects.create_user("odata-owner", password="test")
        OrganizationAccess.objects.create(
            user=self.user, organization=self.organization, role="owner"
        )
        self.client.force_login(self.user)

    def create_draft(self, rows=None, opener=None):
        rows = [profit_row()] if rows is None else rows
        return create_odata_profit_draft(
            "2026-05", "2026-05", self.organization, self.user,
            config=config(), opener=opener or successful_opener(rows),
        )

    def active_batch(self, month=date(2026, 5, 1), revenue="80", cost="30"):
        batch = OneCImportBatch.objects.create(
            organization=self.organization,
            original_filename="active.xlsx",
            stored_file="onec_imports/active.xlsx",
            file_sha256="a" * 64,
            status=OneCImportBatch.STATUS_CONFIRMED,
            uploaded_by=self.user,
            confirmed_by=self.user,
            confirmed_at=timezone.now(),
        )
        OneCMonthlyProfit.objects.create(
            import_batch=batch,
            organization=self.organization,
            period_month=month,
            source_row_number=1,
            nomenclature="Старый товар",
            revenue=revenue,
            cost=cost,
            gross_profit=Decimal(revenue) - Decimal(cost),
        )
        OneCReportPeriodState.objects.create(
            organization=self.organization,
            period_month=month,
            active_batch=batch,
            updated_by=self.user,
        )
        return batch

    def test_draft_saves_private_snapshot_and_no_profit_rows_or_activation(self):
        batch = self.create_draft()
        self.assertEqual(batch.source_type, OneCImportBatch.SOURCE_ODATA)
        self.assertEqual(batch.status, OneCImportBatch.STATUS_PREVIEWED)
        self.assertEqual(batch.rows_detected, 1)
        self.assertEqual(batch.metadata["totals"]["vat"], "10.00")
        self.assertTrue(batch.stored_file.name.endswith(".json"))
        self.assertTrue(batch.stored_file.storage.exists(batch.stored_file.name))
        self.assertEqual(OneCMonthlyProfit.objects.count(), 0)
        self.assertEqual(OneCReportPeriodState.objects.count(), 0)
        with batch.stored_file.open("rb") as source:
            snapshot = json.loads(source.read().decode("utf-8"))
        saved = snapshot["rows"][0]
        self.assertEqual(saved["nomenclature"], "Товар из 1С")
        self.assertEqual(saved["customer_name"], "Покупатель из 1С")
        self.assertEqual(saved["manager_name"], "Ответственный из 1С")
        self.assertEqual(saved["nomenclature_type"], "Запас")
        self.assertEqual(classify_nomenclature_type(saved["nomenclature_type"]), "goods")
        self.assertNotEqual(saved["nomenclature"], ITEM)
        self.assertEqual(saved["source_data"]["recorder"], RECORDER)

    def test_reference_requests_are_get_only_and_use_confirmed_fields(self):
        opener = successful_opener([profit_row()])
        self.create_draft(opener=opener)
        self.assertEqual(len(opener.requests), 4)
        self.assertTrue(all(request.get_method() == "GET" for request, _ in opener.requests))
        reference_urls = [request.full_url for request, _ in opener.requests[1:]]
        self.assertTrue(all("$select=Ref_Key%2CDescription%2CDeletionMark" in url for url in reference_urls))
        self.assertIn("%D0%90%D1%80%D1%82%D0%B8%D0%BA%D1%83%D0%BB", reference_urls[0])
        self.assertIn(
            "%D0%A2%D0%B8%D0%BF%D0%9D%D0%BE%D0%BC%D0%B5%D0%BD%D0%BA%D0%BB%D0%B0%D1%82%D1%83%D1%80%D1%8B",
            reference_urls[0],
        )
        self.assertTrue(all("$filter=Ref_Key%20eq%20guid%27" in url for url in reference_urls))

    def test_reference_guids_are_split_into_bounded_batches(self):
        items = [f"{index:08x}-3333-4333-8333-333333333333" for index in range(41)]
        rows = [
            profit_row(line=index + 1, item=item)
            for index, item in enumerate(items)
        ]
        first_catalog_page = {"value": [
            {
                "Ref_Key": item,
                "Description": f"Товар {index}",
                "DeletionMark": False,
                "Артикул": "",
                "ТипНоменклатуры": "Запас",
            }
            for index, item in enumerate(sorted(items)[:40])
        ]}
        second_catalog_page = {"value": [{
            "Ref_Key": sorted(items)[40], "Description": "Товар 40",
            "DeletionMark": False, "Артикул": "", "ТипНоменклатуры": "Запас",
        }]}
        opener = FakeOpener(
            {"value": rows},
            first_catalog_page,
            second_catalog_page,
            reference_payload(CUSTOMER, "Покупатель"),
            reference_payload(RESPONSIBLE, "Ответственный"),
        )
        batch = self.create_draft(rows=rows, opener=opener)
        self.assertEqual(batch.rows_detected, 41)
        nomenclature_requests = [
            request.full_url for request, _ in opener.requests
            if "Catalog_%D0%9D%D0%BE%D0%BC%D0%B5%D0%BD%D0%BA%D0%BB%D0%B0%D1%82%D1%83%D1%80%D0%B0" in request.full_url
        ]
        self.assertEqual(len(nomenclature_requests), 2)
        self.assertLessEqual(max(url.count("Ref_Key%20eq") for url in nomenclature_requests), 40)

    def test_zero_customer_is_human_label_and_needs_no_catalog_lookup(self):
        zero = "00000000-0000-0000-0000-000000000000"
        rows = [profit_row(customer=zero)]
        opener = FakeOpener(
            {"value": rows},
            reference_payload(ITEM, "Товар", article="A"),
            reference_payload(RESPONSIBLE, "Ответственный"),
        )
        batch = self.create_draft(rows=rows, opener=opener)
        with batch.stored_file.open("rb") as source:
            snapshot = json.loads(source.read().decode("utf-8"))
        self.assertEqual(snapshot["rows"][0]["customer_name"], "Без контрагента")
        self.assertEqual(len(opener.requests), 3)

    def test_nomenclature_types_are_preserved_and_classified(self):
        for source_type, expected in (("Запас", "goods"), ("Услуга", "service")):
            with self.subTest(source_type=source_type):
                opener = FakeOpener(
                    {"value": [profit_row()]},
                    reference_payload(
                        ITEM, "Товар", article="A",
                        nomenclature_type=source_type,
                    ),
                    reference_payload(CUSTOMER, "Покупатель"),
                    reference_payload(RESPONSIBLE, "Ответственный"),
                )
                batch = self.create_draft(opener=opener)
                with batch.stored_file.open("rb") as source:
                    snapshot = json.loads(source.read().decode("utf-8"))
                saved_type = snapshot["rows"][0]["nomenclature_type"]
                self.assertEqual(saved_type, source_type)
                self.assertEqual(classify_nomenclature_type(saved_type), expected)
                batch.stored_file.delete(save=False)
                batch.delete()

    def test_empty_month_replaces_active_version_without_deleting_history(self):
        old = self.active_batch()
        opener = FakeOpener({"value": []})
        batch = self.create_draft(rows=[], opener=opener)
        self.assertEqual(batch.rows_detected, 0)
        self.assertEqual(batch.metadata["scope_months"], ["2026-05-01"])
        self.assertEqual(batch.metadata["monthly"], [{
            "month": "2026-05",
            "row_count": 0,
            "quantity": "0",
            "revenue": "0",
            "vat": "0",
            "cost": "0",
            "gross_profit": "0",
            "has_active": True,
            "active_revenue": "80.00",
            "active_cost": "30.00",
            "active_gross_profit": "50.00",
            "revenue_difference": "-80.00",
            "cost_difference": "-30.00",
            "gross_profit_difference": "-50.00",
        }])
        confirmed = confirm_odata_profit(
            batch.id, self.organization, self.user, config=config()
        )
        state = OneCReportPeriodState.objects.get(period_month=date(2026, 5, 1))
        self.assertEqual(state.active_batch_id, confirmed.id)
        self.assertEqual(
            OneCMonthlyProfit.objects.active_for(self.organization)
            .filter(period_month=date(2026, 5, 1)).count(),
            0,
        )
        self.assertTrue(OneCMonthlyProfit.objects.filter(import_batch=old).exists())
        activation = OneCReportPeriodActivation.objects.get(batch=confirmed)
        self.assertEqual(activation.replaced_batch_id, old.id)

    def test_target_organization_mismatch_fails_before_get_or_batch(self):
        other = Organization.objects.create(name="Other OData target")
        opener = FakeOpener({"value": [profit_row()]})
        with self.assertRaises(ODataDraftError):
            create_odata_profit_draft(
                "2026-05", "2026-05", other, self.user,
                config=config(), opener=opener,
            )
        self.assertEqual(opener.requests, [])
        self.assertEqual(OneCImportBatch.objects.count(), 0)

    def test_missing_or_invalid_target_organization_fails_before_get(self):
        for configured_target in ("", "invalid", "0"):
            with self.subTest(configured_target=configured_target):
                opener = FakeOpener({"value": [profit_row()]})
                with override_settings(
                    ONEC_ODATA_TARGET_ORGANIZATION_ID=configured_target
                ):
                    with self.assertRaises(ODataDraftError):
                        create_odata_profit_draft(
                            "2026-05", "2026-05", self.organization, self.user,
                            config=config(), opener=opener,
                        )
                self.assertEqual(opener.requests, [])
                self.assertEqual(OneCImportBatch.objects.count(), 0)

    def test_service_rejects_more_than_twelve_months_before_get(self):
        opener = FakeOpener({"value": []})
        with self.assertRaises(ODataDraftError):
            create_odata_profit_draft(
                "2025-05", "2026-05", self.organization, self.user,
                config=config(), opener=opener,
            )
        self.assertEqual(opener.requests, [])
        self.assertEqual(OneCImportBatch.objects.count(), 0)

    def test_range_allowlist_and_duplicate_fail_before_database_write(self):
        invalid_payloads = [
            [profit_row(period="2026-06-01T00:00:00+03:00")],
            [profit_row(organization="22222222-2222-2222-2222-222222222222")],
            [profit_row(), profit_row()],
        ]
        for rows in invalid_payloads:
            with self.subTest(rows=rows):
                with self.assertRaises(ODataPreviewError):
                    create_odata_profit_draft(
                        "2026-05", "2026-05", self.organization, self.user,
                        config=config(), opener=FakeOpener({"value": rows}),
                    )
                self.assertEqual(OneCImportBatch.objects.count(), 0)
                self.assertEqual(OneCMonthlyProfit.objects.count(), 0)

    def test_missing_reference_creates_failed_batch_without_profit_rows(self):
        opener = FakeOpener(
            {"value": [profit_row()]},
            {"value": []},
        )
        with self.assertRaises(ODataDraftError) as caught:
            self.create_draft(opener=opener)
        batch = caught.exception.batch
        self.assertIsNotNone(batch)
        self.assertEqual(batch.status, OneCImportBatch.STATUS_FAILED)
        self.assertNotIn(ITEM, batch.error_message)
        self.assertEqual(OneCMonthlyProfit.objects.count(), 0)
        self.assertEqual(OneCReportPeriodState.objects.count(), 0)

    def _assert_deleted_reference_fails(self, opener):
        with self.assertRaises(ODataDraftError) as caught:
            self.create_draft(opener=opener)
        batch = caught.exception.batch
        self.assertEqual(batch.status, OneCImportBatch.STATUS_FAILED)
        self.assertIn("deleted", batch.error_message)
        self.assertEqual(OneCMonthlyProfit.objects.count(), 0)
        self.assertEqual(OneCReportPeriodState.objects.count(), 0)

    def test_deleted_nomenclature_reference_blocks_draft(self):
        self._assert_deleted_reference_fails(FakeOpener(
            {"value": [profit_row()]},
            reference_payload(ITEM, "Удалённый товар", article="A", deletion_mark=True),
        ))

    def test_manual_draft_keeps_deleted_nomenclature_strict_by_default(self):
        with self.assertRaises(ODataDraftError) as caught:
            self.create_draft(opener=FakeOpener(
                {"value": [profit_row()]},
                reference_payload(ITEM, "Удалённый товар", article="A", deletion_mark=True),
            ))
        self.assertEqual(caught.exception.messages, [
            "1C reference is deleted or has an invalid deletion mark",
        ])
        self.assertEqual(caught.exception.batch.status, OneCImportBatch.STATUS_FAILED)
        self.assertEqual(OneCImportBatch.objects.filter(status=OneCImportBatch.STATUS_PREVIEWED).count(), 0)
        self.assertEqual(OneCMonthlyProfit.objects.count(), 0)
        self.assertEqual(OneCReportPeriodState.objects.count(), 0)

    def test_deleted_customer_reference_blocks_draft(self):
        with self.assertRaises(ODataDraftError) as caught:
            self.create_draft(opener=FakeOpener(
                {"value": [profit_row()]},
                reference_payload(ITEM, "Товар", article="A"),
                reference_payload(CUSTOMER, "Удалённый покупатель", deletion_mark=True),
            ))
        self.assertEqual(caught.exception.messages, [
            "1C reference is deleted or has an invalid deletion mark",
        ])
        self.assertEqual(caught.exception.batch.status, OneCImportBatch.STATUS_FAILED)
        self.assertEqual(OneCImportBatch.objects.filter(status=OneCImportBatch.STATUS_PREVIEWED).count(), 0)
        self.assertEqual(OneCMonthlyProfit.objects.count(), 0)
        self.assertEqual(OneCReportPeriodState.objects.count(), 0)

    def test_deleted_responsible_reference_blocks_draft(self):
        self._assert_deleted_reference_fails(FakeOpener(
            {"value": [profit_row()]},
            reference_payload(ITEM, "Товар", article="A"),
            reference_payload(CUSTOMER, "Покупатель"),
            reference_payload(RESPONSIBLE, "Удалённый ответственный", deletion_mark=True),
        ))

    def test_monthly_differences_and_overlap_are_aggregate_only(self):
        self.active_batch()
        rows = [
            profit_row(revenue="100", cost="40"),
            profit_row(
                line=2,
                period="2026-06-15T00:00:00+03:00",
                revenue="50",
                cost="20",
            ),
        ]
        batch = create_odata_profit_draft(
            "2026-05", "2026-06", self.organization, self.user,
            config=config(), opener=successful_opener(rows),
        )
        self.assertEqual(batch.metadata["overlap_months"], ["2026-05"])
        may, june = batch.metadata["monthly"]
        self.assertTrue(may["has_active"])
        self.assertEqual(may["gross_profit_difference"], "10.00")
        self.assertFalse(june["has_active"])
        self.assertIsNone(june["gross_profit_difference"])

    def test_confirm_activates_only_draft_months_and_preserves_history(self):
        old = self.active_batch()
        OneCMonthlyProfit.objects.create(
            import_batch=old,
            organization=self.organization,
            period_month=date(2026, 7, 1),
            source_row_number=2,
            nomenclature="Июльский товар",
            revenue="20",
            cost="10",
            gross_profit="10",
        )
        july_state = OneCReportPeriodState.objects.create(
            organization=self.organization,
            period_month=date(2026, 7, 1),
            active_batch=old,
            updated_by=self.user,
        )
        batch = self.create_draft()
        confirmed = confirm_odata_profit(
            batch.id, self.organization, self.user, config=config()
        )
        self.assertEqual(confirmed.status, OneCImportBatch.STATUS_CONFIRMED)
        row = OneCMonthlyProfit.objects.get(import_batch=confirmed)
        self.assertEqual(str(row.source_recorder), RECORDER)
        self.assertEqual(row.source_row_number, 1)
        self.assertEqual(row.nomenclature, "Товар из 1С")
        self.assertEqual(row.cost_source, OneCMonthlyProfit.COST_SOURCE_ACTUAL)
        self.assertIsNone(row.calculated_cost)
        self.assertIsNone(row.cost_calculation_ratio)
        self.assertEqual(row.analytical_gross_profit, Decimal("60.00"))
        state = OneCReportPeriodState.objects.get(period_month=date(2026, 5, 1))
        self.assertEqual(state.active_batch_id, confirmed.id)
        july_state.refresh_from_db()
        self.assertEqual(july_state.active_batch_id, old.id)
        activation = OneCReportPeriodActivation.objects.get(batch=confirmed)
        self.assertEqual(activation.replaced_batch_id, old.id)
        self.assertTrue(OneCMonthlyProfit.objects.filter(import_batch=old).exists())

    def test_invalid_snapshot_has_no_partial_rows_or_activation(self):
        old = self.active_batch()
        batch = self.create_draft()
        with batch.stored_file.storage.open(batch.stored_file.name, "wb") as target:
            target.write(b'{"tampered":true}')
        with self.assertRaises(ValidationError):
            confirm_odata_profit(batch.id, self.organization, self.user, config=config())
        batch.refresh_from_db()
        self.assertEqual(batch.status, OneCImportBatch.STATUS_FAILED)
        self.assertFalse(OneCMonthlyProfit.objects.filter(import_batch=batch).exists())
        state = OneCReportPeriodState.objects.get(period_month=date(2026, 5, 1))
        self.assertEqual(state.active_batch_id, old.id)
        self.assertFalse(OneCReportPeriodActivation.objects.filter(batch=batch).exists())

    def test_snapshot_validation_rejects_duplicate_identity_even_with_new_checksum(self):
        batch = create_odata_profit_draft(
            "2026-05", "2026-05", self.organization, self.user,
            config=config(),
            opener=successful_opener([profit_row(), profit_row(line=2)]),
        )
        with batch.stored_file.open("rb") as source:
            snapshot = json.loads(source.read().decode("utf-8"))
        snapshot["rows"][1]["source_row_number"] = 1
        snapshot["rows"][1]["source_data"]["line_number"] = 1
        content = json.dumps(
            snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        with batch.stored_file.storage.open(batch.stored_file.name, "wb") as target:
            target.write(content)
        batch.file_sha256 = hashlib.sha256(content).hexdigest()
        batch.save(update_fields=["file_sha256"])
        with self.assertRaises(ValidationError):
            confirm_odata_profit(batch.id, self.organization, self.user, config=config())
        self.assertFalse(OneCMonthlyProfit.objects.filter(import_batch=batch).exists())
        self.assertFalse(OneCReportPeriodState.objects.exists())

    def test_activation_failure_rolls_back_created_rows_and_state_changes(self):
        old = self.active_batch()
        batch = self.create_draft()
        with patch(
            "pool_service.finance_imports.odata_profit_drafts._activate_period_states",
            side_effect=RuntimeError("activation failed"),
        ):
            with self.assertRaises(RuntimeError):
                confirm_odata_profit(batch.id, self.organization, self.user, config=config())
        self.assertFalse(OneCMonthlyProfit.objects.filter(import_batch=batch).exists())
        state = OneCReportPeriodState.objects.get(period_month=date(2026, 5, 1))
        self.assertEqual(state.active_batch_id, old.id)
        self.assertFalse(OneCReportPeriodActivation.objects.filter(batch=batch).exists())

    def test_xlsx_is_default_source_and_existing_constraint_remains(self):
        batch = OneCImportBatch.objects.create(
            organization=self.organization,
            original_filename="regression.xlsx",
            stored_file="onec_imports/regression.xlsx",
            file_sha256="b" * 64,
            uploaded_by=self.user,
        )
        self.assertEqual(batch.source_type, OneCImportBatch.SOURCE_XLSX)
        self.assertTrue(any(
            constraint.name == "unique_onec_batch_source_identity"
            for constraint in OneCMonthlyProfit._meta.constraints
        ))

    def test_duplicate_odata_identity_is_rejected_and_different_recorder_is_allowed(self):
        batch = OneCImportBatch.objects.create(
            organization=self.organization,
            source_type=OneCImportBatch.SOURCE_ODATA,
            original_filename="identity.json",
            stored_file="onec_imports/identity.json",
            file_sha256="d" * 64,
            uploaded_by=self.user,
        )
        common = {
            "import_batch": batch,
            "organization": self.organization,
            "period_month": date(2026, 5, 1),
            "source_row_number": 1,
            "nomenclature": "Товар",
        }
        uppercase_recorder = "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF"
        normalized_recorder = uppercase_recorder.lower()
        first = OneCMonthlyProfit.objects.create(source_recorder=uppercase_recorder, **common)
        other_recorder = "77777777-7777-4777-8777-777777777777"
        second = OneCMonthlyProfit.objects.create(source_recorder=other_recorder, **common)
        self.assertEqual(first.source_identity, f"odata:{normalized_recorder}:1")
        self.assertEqual(second.source_identity, f"odata:{other_recorder}:1")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OneCMonthlyProfit.objects.create(source_recorder=normalized_recorder, **common)

    def test_xlsx_identity_is_unique_by_month_and_row(self):
        batch = OneCImportBatch.objects.create(
            organization=self.organization,
            original_filename="identity.xlsx",
            stored_file="onec_imports/identity.xlsx",
            file_sha256="e" * 64,
            uploaded_by=self.user,
        )
        common = {
            "import_batch": batch,
            "organization": self.organization,
            "source_row_number": 7,
            "nomenclature": "Товар",
        }
        may = OneCMonthlyProfit.objects.create(period_month=date(2026, 5, 1), **common)
        june = OneCMonthlyProfit.objects.create(period_month=date(2026, 6, 1), **common)
        self.assertEqual(may.source_identity, "xlsx:2026-05-01:7")
        self.assertEqual(june.source_identity, "xlsx:2026-06-01:7")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                OneCMonthlyProfit.objects.create(period_month=date(2026, 5, 1), **common)

    def test_model_and_migration_unique_constraints_have_no_condition(self):
        constraint = next(
            item for item in OneCMonthlyProfit._meta.constraints
            if item.name == "unique_onec_batch_source_identity"
        )
        self.assertIsNone(constraint.condition)
        migration = importlib.import_module(
            "pool_service.migrations.0095_onec_odata_draft_fields"
        )
        self.assertNotIn("condition=", inspect.getsource(migration))

    def test_form_defaults_to_current_month_and_eleven_previous(self):
        form = ODataProfitDraftForm()
        start = form.fields["start_month"].initial
        end = form.fields["end_month"].initial
        self.assertEqual((end.year * 12 + end.month) - (start.year * 12 + start.month), 11)
        self.assertEqual(start.day, 1)
        self.assertEqual(end.day, 1)

    def test_form_rejects_more_than_twelve_months(self):
        form = ODataProfitDraftForm(data={
            "start_month": "2025-05",
            "end_month": "2026-05",
        })
        self.assertFalse(form.is_valid())
        self.assertIn("не более 12 месяцев", str(form.non_field_errors()))

    def test_button_is_hidden_for_non_target_organization(self):
        other = Organization.objects.create(
            name="Other UI organization",
            paid_until=timezone.now() + timedelta(days=30),
        )
        OrganizationAccess.objects.filter(user=self.user).update(organization=other)
        response = self.client.get(reverse("finance_onec_import_list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Получить из 1С")

    @patch("pool_service.finance_views.create_odata_profit_draft")
    def test_ui_create_is_post_only_and_uses_existing_permission(self, create_mock):
        batch = OneCImportBatch.objects.create(
            organization=self.organization,
            source_type=OneCImportBatch.SOURCE_ODATA,
            original_filename="draft.json",
            stored_file="onec_imports/draft.json",
            file_sha256="c" * 64,
            uploaded_by=self.user,
            status=OneCImportBatch.STATUS_PREVIEWED,
        )
        create_mock.return_value = batch
        url = reverse("finance_onec_odata_profit_draft")
        self.assertEqual(self.client.get(url).status_code, 405)
        response = self.client.post(url, {
            "start_month": "2026-05", "end_month": "2026-05",
        })
        self.assertRedirects(
            response, reverse("finance_onec_import_preview", args=[batch.id])
        )
        create_mock.assert_called_once_with(
            "2026-05", "2026-05", self.organization, self.user
        )

        manager = User.objects.create_user("odata-manager", password="test")
        OrganizationAccess.objects.create(
            user=manager, organization=self.organization, role="manager"
        )
        self.client.force_login(manager)
        self.assertEqual(self.client.post(url, {
            "start_month": "2026-05", "end_month": "2026-05",
        }).status_code, 403)

    def test_confirm_view_routes_odata_to_separate_service(self):
        batch = self.create_draft()
        with patch("pool_service.finance_views.confirm_odata_profit", return_value=batch) as confirm_mock:
            response = self.client.post(
                reverse("finance_onec_import_confirm", args=[batch.id])
            )
        self.assertEqual(response.status_code, 302)
        confirm_mock.assert_called_once_with(batch.id, self.organization, self.user)


class ODataProfitIdentityMigrationTests(TransactionTestCase):
    migrate_from = [("pool_service", "0094_mysql_identity_unique_constraints")]
    migrate_to = [("pool_service", "0095_onec_odata_draft_fields")]

    def setUp(self):
        super().setUp()
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_from)
        old_apps = self.executor.loader.project_state(self.migrate_from).apps
        Organization = old_apps.get_model("pool_service", "Organization")
        User = old_apps.get_model("auth", "User")
        Batch = old_apps.get_model("pool_service", "OneCImportBatch")
        MonthlyProfit = old_apps.get_model("pool_service", "OneCMonthlyProfit")
        organization = Organization.objects.create(name="OData identity migration")
        user = User.objects.create(username="migration-owner")
        batch = Batch.objects.create(
            organization=organization,
            original_filename="historical.xlsx",
            stored_file="onec_imports/historical.xlsx",
            file_sha256="f" * 64,
            uploaded_by=user,
        )
        MonthlyProfit.objects.create(
            import_batch=batch,
            organization=organization,
            period_month=date(2026, 5, 1),
            source_row_number=7,
            nomenclature="Май",
        )
        MonthlyProfit.objects.create(
            import_batch=batch,
            organization=organization,
            period_month=date(2026, 6, 1),
            source_row_number=7,
            nomenclature="Июнь",
        )

    def tearDown(self):
        MigrationExecutor(connection).migrate(
            MigrationExecutor(connection).loader.graph.leaf_nodes()
        )
        super().tearDown()

    def test_0095_backfills_xlsx_identity_and_creates_plain_unique_constraint(self):
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.migrate_to)
        apps = self.executor.loader.project_state(self.migrate_to).apps
        MonthlyProfit = apps.get_model("pool_service", "OneCMonthlyProfit")
        identities = list(
            MonthlyProfit.objects.order_by("period_month")
            .values_list("source_identity", flat=True)
        )
        self.assertEqual(identities, [
            "xlsx:2026-05-01:7",
            "xlsx:2026-06-01:7",
        ])
        field = MonthlyProfit._meta.get_field("source_identity")
        self.assertFalse(field.null)
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(
                cursor, MonthlyProfit._meta.db_table
            )
        self.assertTrue(
            constraints["unique_onec_batch_source_identity"].get("unique")
        )
