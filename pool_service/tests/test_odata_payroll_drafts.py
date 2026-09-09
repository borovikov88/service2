"""Synthetic financial data only; no live 1C or production database access."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
import json
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from pool_service.finance_imports.odata_payroll_drafts import (
    REPORT_TYPE, classify_preview, config_from_settings, create_odata_payroll_draft,
    confirm_odata_payroll, payroll_accrual_confirmation_state, read_for_draft,
)
from pool_service.finance_imports.odata_payroll import PayrollError
from pool_service.finance_imports.services import DuplicateImportError
from pool_service.models import (DataAuditLog, OneCImportBatch, OneCReportPeriodState,
    Organization, OrganizationAccess, PayrollAccrualMonth, PayrollRow)

ORG = "00000000-0000-0000-0000-000000000001"
OTHER_ORG = "00000000-0000-0000-0000-000000000002"
CURRENCY = "00000000-0000-0000-0000-000000000003"
MONTH = "2025-04"
PERIOD = date(2025, 4, 1)
PATCH_READER = "pool_service.finance_imports.odata_payroll_drafts.read_for_draft"


def fixture(value="150.25"):
    group = {"organization_guid": ORG, "currency_guid": CURRENCY, "type_value": "Начисление",
        "rows": 2, "amount": value, "amount_currency": value,
        "non_cent_rows": 0, "period_month_differs_rows": 1}
    control = {"organization_guid": ORG, "currency_guid": CURRENCY, "record_type": "Receipt",
        "recorder_type": "StandardODATA.Document_НачислениеЗарплатыУНФ", "rows": 2,
        "amount": value, "amount_currency": value, "non_cent_rows": 0}
    return {"kind": "unclassified_monthly_payroll_preview", "month": MONTH,
        "period_basis": "ПериодРегистрации", "status": "data_present", "rows": 2,
        "selected_organizations": [ORG, OTHER_ORG], "organizations_without_rows": [OTHER_ORG],
        "groups": [group], "settlements": {"status": "data_present", "groups": [control]}}


class PayrollAccrualDraftTests(TestCase):
    def setUp(self):
        self.private = TemporaryDirectory()
        self.addCleanup(self.private.cleanup)
        self.organization = Organization.objects.create(name="Synthetic", paid_until=timezone.now() + timedelta(days=30))
        self.other = Organization.objects.create(name="Other", paid_until=timezone.now() + timedelta(days=30))
        self.user = User.objects.create_user("payroll-owner")
        OrganizationAccess.objects.create(organization=self.organization, user=self.user, role="owner")
        self.config = override_settings(PRIVATE_MEDIA_ROOT=self.private.name,
            ONEC_ODATA_BASE_URL="https://example.test/test/odata/standard.odata/",
            ONEC_ODATA_USERNAME="test-user", ONEC_ODATA_PASSWORD="test-secret",
            ONEC_ODATA_TARGET_ORGANIZATION_ID=str(self.organization.pk),
            ONEC_ODATA_ORGANIZATION_GUIDS=(ORG, OTHER_ORG),
            ONEC_ODATA_PAYROLL_CURRENCY_GUID=CURRENCY, ONEC_ODATA_PAYROLL_CURRENCY_CODE="RUB")
        self.config.enable()
        self.addCleanup(self.config.disable)
        self.client.force_login(self.user)

    def draft(self, value="150.25", preview=None):
        with patch(PATCH_READER, return_value=preview or fixture(value)):
            return create_odata_payroll_draft(MONTH, self.organization, self.user)

    def confirm(self, batch):
        return confirm_odata_payroll(batch.pk, self.organization, self.user, confirm_coverage=True)

    def test_preview_does_not_activate_then_confirmation_is_idempotent(self):
        batch = self.draft()
        self.assertFalse(PayrollAccrualMonth.objects.exists())
        self.assertFalse(OneCReportPeriodState.objects.exists())
        self.assertTrue(payroll_accrual_confirmation_state(batch, self.organization)["can_confirm"])
        self.confirm(batch)
        self.confirm(batch)
        row = PayrollAccrualMonth.objects.get()
        self.assertEqual(row.accrued, Decimal("150.25"))
        self.assertEqual(row.source_organization_guids, [ORG])
        self.assertEqual(row.source_rows, 2)
        self.assertFalse(PayrollRow.objects.exists())
        state = OneCReportPeriodState.objects.get()
        self.assertEqual(state.active_batch_id, batch.pk)
        self.assertTrue(DataAuditLog.objects.filter(after__coverage_confirmed=True).exists())

    def test_missing_coverage_acknowledgement_cannot_confirm(self):
        batch = self.draft()
        for value in (False, None, "true", 1):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                confirm_odata_payroll(batch.pk, self.organization, self.user, confirm_coverage=value)
        self.assertFalse(PayrollAccrualMonth.objects.exists())

    def test_replacement_not_sum_and_retry_of_superseded_batch_does_not_reactivate(self):
        a = self.draft()
        self.confirm(a)
        b = self.draft("180.00")
        self.confirm(b)
        self.confirm(a)
        rows = PayrollAccrualMonth.objects.active_for(self.organization, REPORT_TYPE)
        self.assertEqual(list(rows.values_list("accrued", flat=True)), [Decimal("180.00")])
        self.assertEqual(PayrollAccrualMonth.objects.count(), 2)

    def test_unchanged_scheduled_read_reuses_active_batch(self):
        batch = self.draft()
        self.confirm(batch)
        with self.assertRaises(DuplicateImportError) as result:
            self.draft()
        self.assertEqual(result.exception.batch.pk, batch.pk)
        self.assertEqual(OneCImportBatch.objects.count(), 1)

    def test_outdated_odata_and_excel_baselines_block(self):
        a = self.draft()
        b = self.draft("180.00")
        self.confirm(b)
        with self.assertRaises(ValidationError):
            self.confirm(a)
        c = self.draft("190.00")
        xlsx = OneCImportBatch.objects.create(organization=self.organization, import_type=OneCImportBatch.TYPE_PAYROLL,
            uploaded_by=self.user, status=OneCImportBatch.STATUS_CONFIRMED, file_sha256="f" * 64)
        OneCReportPeriodState.objects.create(organization=self.organization, report_type=OneCImportBatch.TYPE_PAYROLL,
                                            period_month=PERIOD, active_batch=xlsx)
        with self.assertRaises(ValidationError):
            self.confirm(c)
        self.assertFalse(payroll_accrual_confirmation_state(c, self.organization)["can_confirm"])

    @override_settings(ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS="00000000-0000-0000-0000-000000000091")
    def test_withholding_scope_change_blocks_confirmation(self):
        batch = self.draft(preview=withholding_fixture())
        with override_settings(ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS=""), self.assertRaises(ValidationError):
            self.confirm(batch)
        self.assertFalse(PayrollAccrualMonth.objects.exists())

    @override_settings(ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS="00000000-0000-0000-0000-000000000091")
    def test_withholding_confirmation_persists_gross(self):
        self.confirm(self.draft(preview=withholding_fixture()))
        self.assertEqual(PayrollAccrualMonth.objects.get().accrued, Decimal("180.00"))

    def test_snapshot_tamper_config_change_and_metadata_not_authoritative(self):
        batch = self.draft()
        batch.metadata["summary"]["accrued"] = "999999"
        batch.save(update_fields=["metadata"])
        self.assertEqual(payroll_accrual_confirmation_state(batch, self.organization)["summary"]["accrued"], "150.25")
        with override_settings(ONEC_ODATA_PAYROLL_CURRENCY_GUID=OTHER_ORG), self.assertRaises(ValidationError):
            self.confirm(batch)
        with batch.stored_file.open("wb") as target:
            target.write(b"{}")
        with self.assertRaises(ValidationError):
            self.confirm(batch)
        self.assertFalse(PayrollAccrualMonth.objects.exists())

    def test_bad_source_does_not_replace_active(self):
        current = self.draft()
        self.confirm(current)
        invalid = []
        empty = fixture(); empty.update(status="missing", groups=[], rows=0); invalid.append(empty)
        mixed = fixture(); mixed["groups"][0]["currency_guid"] = OTHER_ORG; invalid.append(mixed)
        unknown = fixture(); unknown["groups"][0]["type_value"] = "Удержание"; invalid.append(unknown)
        mismatch = fixture(); mismatch["settlements"]["groups"][0]["amount"] = "1.00"; invalid.append(mismatch)
        fractional = fixture("1.001"); invalid.append(fractional)
        control = fixture(); control["settlements"]["groups"][0]["recorder_type"] = "OtherDocument"; invalid.append(control)
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValidationError):
                self.draft(preview=item)
        self.assertEqual(OneCImportBatch.objects.count(), 1)
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch_id, current.pk)

    def test_signed_adjustment_and_no_row_org_are_preserved(self):
        batch = self.draft("-10.25")
        self.confirm(batch)
        self.assertEqual(PayrollAccrualMonth.objects.get().accrued, Decimal("-10.25"))
        self.assertEqual(batch.metadata["summary"]["organizations_without_rows"], [OTHER_ORG])

    def test_authorization_scope_and_expired_organization(self):
        with patch(PATCH_READER) as reader:
            with self.assertRaises(PermissionDenied):
                create_odata_payroll_draft(MONTH, self.other, self.user)
            reader.assert_not_called()
        batch = self.draft()
        self.organization.paid_until = timezone.now() - timedelta(days=1)
        self.organization.save(update_fields=["paid_until"])
        with self.assertRaises(PermissionDenied):
            self.confirm(batch)
        self.assertFalse(PayrollAccrualMonth.objects.exists())

    def test_role_revocation_during_read_prevents_snapshot(self):
        def revoke(*args):
            OrganizationAccess.objects.filter(user=self.user).delete()
            return fixture()
        with patch(PATCH_READER, side_effect=revoke), self.assertRaises(PermissionDenied):
            create_odata_payroll_draft(MONTH, self.organization, self.user)
        self.assertFalse(OneCImportBatch.objects.exists())

    def test_missing_rub_binding_blocks_before_network(self):
        with override_settings(ONEC_ODATA_PAYROLL_CURRENCY_CODE=""), patch(PATCH_READER) as reader, self.assertRaises(ValidationError):
            create_odata_payroll_draft(MONTH, self.organization, self.user)
        reader.assert_not_called()

    def test_ui_get_never_imports_and_post_needs_coverage(self):
        fetch = reverse("finance_payroll_accrual_fetch")
        with patch(PATCH_READER) as reader:
            self.assertEqual(self.client.get(fetch).status_code, 200)
            reader.assert_not_called()
        batch = self.draft()
        url = reverse("finance_payroll_accrual_preview", args=[batch.pk])
        response = self.client.get(url)
        self.assertContains(response, "не подтверждает нулевой ФОТ")
        self.client.post(url, {})
        self.assertFalse(PayrollAccrualMonth.objects.exists())
        self.assertEqual(self.client.post(url, {"confirm_coverage": "on"}).status_code, 302)
        self.assertTrue(PayrollAccrualMonth.objects.exists())

    def test_other_organization_batch_is_404(self):
        batch = self.draft()
        batch.organization = self.other; batch.save(update_fields=["organization"])
        self.assertEqual(self.client.get(reverse("finance_payroll_accrual_preview", args=[batch.pk])).status_code, 404)

    def test_command_defaults_to_preview_and_confirm_requires_ack(self):
        args = {"organization": self.organization.pk, "user": self.user.pk, "month": MONTH}
        with patch(PATCH_READER, return_value=fixture()):
            call_command("sync_onec_payroll", **args, stdout=StringIO())
        self.assertFalse(PayrollAccrualMonth.objects.exists())
        with self.assertRaises(CommandError):
            call_command("sync_onec_payroll", **args, confirm=True, stdout=StringIO())
        with patch(PATCH_READER, return_value=fixture()):
            call_command("sync_onec_payroll", **args, confirm=True, accept_coverage=True, stdout=StringIO())
        self.assertTrue(PayrollAccrualMonth.objects.exists())

    def test_changed_source_scope_allows_fresh_draft_after_confirmation(self):
        old = self.draft()
        self.confirm(old)
        with override_settings(ONEC_ODATA_BASE_URL="https://example.test/changed/odata/standard.odata/"):
            fresh = self.draft("175.00")
            self.confirm(fresh)
        self.assertNotEqual(old.pk, fresh.pk)
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch_id, fresh.pk)

    def test_missing_old_snapshot_allows_recovery_from_new_read(self):
        old = self.draft()
        self.confirm(old)
        old.stored_file.storage.delete(old.stored_file.name)
        fresh = self.draft("175.00")
        self.confirm(fresh)
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch_id, fresh.pk)

    def test_disabled_actor_during_read_cannot_save(self):
        def disable(*args):
            User.objects.filter(pk=self.user.pk).update(is_active=False)
            return fixture()
        with patch(PATCH_READER, side_effect=disable), self.assertRaises(PermissionDenied):
            create_odata_payroll_draft(MONTH, self.organization, self.user)
        self.assertFalse(OneCImportBatch.objects.exists())

    def test_worker_timeout_and_secrets_never_in_argv(self):
        with patch("pool_service.finance_imports.odata_payroll_drafts.subprocess.run", side_effect=subprocess.TimeoutExpired("worker", 65)) as run:
            with self.assertRaises(PayrollError):
                read_for_draft(config_from_settings(), MONTH)
        argv = run.call_args.args[0]
        self.assertNotIn("test-secret", " ".join(argv))
        self.assertEqual(run.call_args.kwargs["env"]["ONEC_ODATA_PASSWORD"], "test-secret")
        self.assertEqual(run.call_args.kwargs["timeout"], 65)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)


WITHHOLDING_KIND = "00000000-0000-0000-0000-000000000091"
ACCRUAL_KIND = "00000000-0000-0000-0000-000000000092"


def withholding_fixture():
    data = fixture("180.00")
    accrual = data["groups"][0]
    accrual.update(negative_amount_rows=0, negative_currency_amount_rows=0)
    deduction = dict(accrual, type_value="Налог", rows=1, amount="30.00", amount_currency="30.00", period_month_differs_rows=0)
    data["groups"].append(deduction)
    data["rows"] = 3
    data["kind_groups"] = [dict(accrual, kind_guid=ACCRUAL_KIND), dict(deduction, kind_guid=WITHHOLDING_KIND)]
    data["settlements"]["groups"][0].update(amount="150.00", amount_currency="150.00", rows=3,
        positive_amount="180.00", positive_amount_currency="180.00", negative_amount="-30.00", negative_amount_currency="-30.00", negative_amount_rows=1, negative_currency_amount_rows=1)
    return data


class ConfirmedWithholdingTests(TestCase):
    def classify(self, data):
        return classify_preview(data, MONTH, [ORG, OTHER_ORG], CURRENCY)

    @override_settings(ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS=WITHHOLDING_KIND)
    def test_gross_not_net_and_snapshot_unchanged(self):
        data = withholding_fixture()
        original = deepcopy(data)
        self.assertEqual(self.classify(data)["accrued"], "180.00")
        self.assertEqual(data, original)

    @override_settings(ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS="")
    def test_unconfirmed_kind_blocks(self):
        with self.assertRaises(ValidationError):
            self.classify(withholding_fixture())

    @override_settings(ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS=WITHHOLDING_KIND)
    def test_mutated_evidence_blocks(self):
        mutations = [
            lambda d: d.pop("kind_groups"),
            lambda d: d["kind_groups"][1].update(kind_guid=ACCRUAL_KIND),
            lambda d: d["kind_groups"][1].update(amount="31"),
            lambda d: d["kind_groups"][1].update(negative_amount_rows=1),
            lambda d: d["kind_groups"][1].update(currency_guid=OTHER_ORG),
            lambda d: d["kind_groups"][1].update(organization_guid=OTHER_ORG),
            lambda d: d["kind_groups"][1].update(type_value="Неизвестно"),
            lambda d: d["settlements"]["groups"][0].update(positive_amount="190", negative_amount="-40"),
            lambda d: d["settlements"]["groups"][0].update(amount="180"),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                data = withholding_fixture()
                mutate(data)
                with self.assertRaises(ValidationError):
                    self.classify(data)

    @override_settings(ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS=WITHHOLDING_KIND)
    def test_confirmed_kind_cannot_become_accrual(self):
        data = withholding_fixture()
        data["groups"] = [dict(data["groups"][0], rows=3, amount="210.00", amount_currency="210.00")]
        data["kind_groups"][1]["type_value"] = "Начисление"
        data["settlements"]["groups"][0].update(amount="210", amount_currency="210", positive_amount="210", positive_amount_currency="210", negative_amount="0", negative_amount_currency="0")
        with self.assertRaises(ValidationError):
            self.classify(data)

    def test_scope_changes_on_kind_allowlist_change(self):
        from pool_service.finance_imports.odata_payroll_drafts import _scope
        config = {"ONEC_ODATA_ORGANIZATION_GUIDS": ORG, "ONEC_ODATA_PAYROLL_CURRENCY_CODE": "RUB", "ONEC_ODATA_PAYROLL_CURRENCY_GUID": CURRENCY}
        before = _scope(config)[2]
        config["ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS"] = WITHHOLDING_KIND
        self.assertNotEqual(before, _scope(config)[2])
        config["ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS"] = "invalid"
        with self.assertRaises(ValidationError):
            _scope(config)
