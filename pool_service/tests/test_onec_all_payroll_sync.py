"""All fixtures are synthetic; never contact production or 1C."""
from datetime import date
from unittest.mock import patch
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from pool_service.tests import test_odata_payroll_drafts as fixtures
from pool_service.tests.test_odata_payroll_drafts import fixture, ORG, OTHER_ORG, MONTH, PERIOD, PATCH_READER
from pool_service.finance_imports import odata_unified_sync as sync
from pool_service.tests.test_onec_odata_unified_sync import profit_row, cashflow_row
from pool_service.models import OneCODataSyncRun, PayrollAccrualMonth, OneCReportPeriodState


class AllPayrollSyncTests(TestCase):
    setUp = fixtures.PayrollAccrualDraftTests.setUp

    def start(self, reports=None):
        return sync.start_unified_sync(self.organization, self.user, reports or [sync.REPORT_PAYROLL],
            mode=OneCODataSyncRun.MODE_AUTO_APPLY, period_start=PERIOD, period_end=PERIOD)[0]

    def step(self, run):
        run.refresh_from_db()
        return sync.step_unified_sync(run.pk, self.user, sync.SUPPORTED_REPORT_TYPES,
            run.cursor['version'], mode=OneCODataSyncRun.MODE_AUTO_APPLY)

    def coverage(self):
        return override_settings(ONEC_ODATA_PAYROLL_AUTO_COVERAGE_GUIDS=f'{ORG},{OTHER_ORG}')

    def profit_rows(self):
        return [profit_row(MONTH, organization_guid=ORG)]

    def cashflow_rows(self):
        row = cashflow_row(MONTH)
        row['source_data']['organization_guid'] = ORG
        return [row]

    @override_settings(ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS=fixtures.WITHHOLDING_KIND)
    def test_confirmed_withholding_auto_applies_gross(self):
        with self.coverage(), patch(PATCH_READER, return_value=fixtures.withholding_fixture()):
            result = self.step(self.start())
        self.assertEqual(result.progress['outcome'], 'applied')
        self.assertEqual(str(PayrollAccrualMonth.objects.get().accrued), '180.00')

    @override_settings(ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS=fixtures.WITHHOLDING_KIND)
    def test_withholding_config_change_blocks_auto_run(self):
        with self.coverage():
            run = self.start()
            with override_settings(ONEC_ODATA_PAYROLL_WITHHOLDING_GUIDS=''), patch(PATCH_READER) as reader:
                result = self.step(run)
                reader.assert_not_called()
        self.assertEqual(result.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertFalse(PayrollAccrualMonth.objects.exists())

    def test_requires_explicit_coverage(self):
        with self.assertRaises(ValidationError):
            self.start()

    def test_payroll_applies_and_repeated_run_does_not_duplicate(self):
        with self.coverage(), patch(PATCH_READER, return_value=fixture()):
            first = self.step(self.start())
            self.assertEqual(first.progress['outcome'], 'applied')
            second = self.step(self.start())
            self.assertEqual(second.progress['outcome'], 'no_change')
        self.assertEqual(PayrollAccrualMonth.objects.count(), 1)

    def test_all_sources_apply_only_at_end(self):
        with self.coverage(), patch(PATCH_READER, return_value=fixture()), \
             patch.object(sync, '_collect_profit_chunk', return_value=(self.profit_rows(), 1)), \
             patch.object(sync, '_collect_cashflow_chunk', return_value=(self.cashflow_rows(), 1, [])):
            run = self.start(sync.SUPPORTED_REPORT_TYPES)
            self.step(run)
            self.assertFalse(OneCReportPeriodState.objects.exists())
            self.step(run)
            self.assertFalse(OneCReportPeriodState.objects.exists())
            result = self.step(run)
            self.assertEqual(result.progress.get('outcome'), 'applied')
        self.assertEqual(OneCReportPeriodState.objects.count(), 3)
        self.assertEqual(sync.OneCMonthlyProfit.objects.count(), 1)
        self.assertEqual(sync.CashFlowRow.objects.count(), 1)

    def test_payroll_failure_preserves_all_sources(self):
        with self.coverage(), patch(PATCH_READER, side_effect=ValueError), \
             patch.object(sync, '_collect_profit_chunk', return_value=([], 1)), \
             patch.object(sync, '_collect_cashflow_chunk', return_value=([], 1, [])):
            run = self.start(sync.SUPPORTED_REPORT_TYPES)
            self.step(run)
            self.step(run)
            result = self.step(run)
            self.assertEqual(result.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertFalse(OneCReportPeriodState.objects.exists())

    def test_missing_month_preserves_old_payroll(self):
        empty = {'kind': 'unclassified_monthly_payroll_preview', 'month': MONTH,
            'period_basis': 'ПериодРегистрации', 'status': 'missing', 'rows': 0,
            'selected_organizations': [ORG, OTHER_ORG], 'organizations_without_rows': [ORG, OTHER_ORG],
            'groups': [], 'settlements': {'status': 'missing', 'rows': 0, 'groups': []}}
        with self.coverage(), patch(PATCH_READER, return_value=fixture()):
            self.step(self.start())
        state = OneCReportPeriodState.objects.get().active_batch_id
        with self.coverage(), patch(PATCH_READER, return_value=empty):
            result = self.step(self.start())
        self.assertEqual(result.progress['outcome'], 'no_change')
        self.assertEqual(result.result_summary[sync.REPORT_PAYROLL]['missing_preserved_months'], [MONTH])
        self.assertEqual(OneCReportPeriodState.objects.get().active_batch_id, state)
        response = self.client.get(reverse('finance_onec_import_list'))
        self.assertContains(response, 'актуальность не подтверждена за')
        self.assertContains(response, MONTH)

    def test_retry_after_late_failure_keeps_separate_snapshots(self):
        with self.coverage(), \
             patch.object(sync, '_collect_profit_chunk', return_value=(self.profit_rows(), 1)), \
             patch.object(sync, '_collect_cashflow_chunk', return_value=(self.cashflow_rows(), 1, [])):
            first = self.start(sync.SUPPORTED_REPORT_TYPES)
            self.step(first)
            self.step(first)
            with patch(PATCH_READER, side_effect=ValueError):
                self.assertEqual(self.step(first).status, OneCODataSyncRun.STATUS_FAILED)
            old_batches = list(sync.OneCImportBatch.objects.filter(sync_run=first))
            self.assertEqual(len(old_batches), 2)
            sync.delete_private_batch_file(old_batches[0])
            second = self.start(sync.SUPPORTED_REPORT_TYPES)
            self.step(second)
            self.step(second)
            with patch(PATCH_READER, return_value=fixture()):
                self.assertEqual(self.step(second).progress['outcome'], 'applied')
            for old in old_batches:
                current = sync.OneCImportBatch.objects.get(sync_run=second, import_type=old.import_type)
                self.assertNotEqual(old.file_sha256, current.file_sha256)
                self.assertEqual(old.metadata['month_fingerprint'], current.metadata['month_fingerprint'])
                old.refresh_from_db()
                self.assertEqual(old.sync_run_id, first.pk)
                self.assertEqual(old.status, sync.OneCImportBatch.STATUS_PREVIEWED)
            third = self.start(sync.SUPPORTED_REPORT_TYPES)
            self.step(third)
            self.step(third)
            with patch(PATCH_READER, return_value=fixture()):
                self.assertEqual(self.step(third).progress['outcome'], 'no_change')
        self.assertEqual(sync.OneCMonthlyProfit.objects.count(), 1)
        self.assertEqual(sync.CashFlowRow.objects.count(), 1)

    def test_apply_failure_rolls_back_payroll_and_other_sources(self):
        def fail(stage):
            if stage == 'payroll_rows':
                raise RuntimeError('synthetic')
        with self.coverage(), patch(PATCH_READER, return_value=fixture()), \
             patch.object(sync, '_collect_profit_chunk', return_value=(self.profit_rows(), 1)), \
             patch.object(sync, '_collect_cashflow_chunk', return_value=(self.cashflow_rows(), 1, [])), \
             patch.object(sync, '_auto_apply_fault', side_effect=fail):
            run = self.start(sync.SUPPORTED_REPORT_TYPES)
            self.step(run)
            self.step(run)
            result = self.step(run)
        self.assertEqual(result.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertFalse(OneCReportPeriodState.objects.exists())
        self.assertFalse(PayrollAccrualMonth.objects.exists())
        self.assertFalse(sync.OneCMonthlyProfit.objects.exists())
        self.assertFalse(sync.CashFlowRow.objects.exists())

    def test_revoked_user_rejected_even_stale_user_object(self):
        with self.coverage():
            type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
            with self.assertRaises(PermissionDenied):
                self.start()

    def test_second_start_reuses_same_run(self):
        with self.coverage():
            first = self.start()
            second = self.start()
        self.assertEqual(first.pk, second.pk)

    def test_worker_can_resume_interruption_after_collection(self):
        with self.coverage(), patch(PATCH_READER, return_value=fixture()):
            run = self.start()
            with patch.object(sync, 'apply_auto_sync', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.step(run)
            run.refresh_from_db()
            self.assertEqual(run.status, OneCODataSyncRun.STATUS_RUNNING)
            self.assertEqual(run.progress['step_state'], 'apply_pending')
            self.assertFalse(PayrollAccrualMonth.objects.exists())
            result = self.step(run)
            self.assertEqual(result.progress['outcome'], 'applied')

    def test_changed_coverage_fails_before_apply(self):
        with self.coverage(), patch(PATCH_READER, return_value=fixture()):
            run = self.start()
            with patch.object(sync, 'apply_auto_sync', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.step(run)
        with override_settings(ONEC_ODATA_PAYROLL_AUTO_COVERAGE_GUIDS=''):
            result = self.step(run)
        self.assertEqual(result.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertFalse(PayrollAccrualMonth.objects.exists())

    def test_late_failure_cannot_overwrite_success(self):
        with self.coverage(), patch(PATCH_READER, return_value=fixture()):
            run = self.step(self.start())
            result = sync._fail_auto_run(run.pk, 'stale_worker', 'late failure')
        self.assertEqual(result.status, OneCODataSyncRun.STATUS_COMPLETED)
        self.assertEqual(result.progress['outcome'], 'applied')

    def test_terminal_step_and_history_do_not_expose_payroll_without_permission(self):
        with self.coverage(), patch(PATCH_READER, return_value=fixture()):
            run = self.step(self.start())
        with patch('pool_service.finance_views.can_import_payroll', return_value=False):
            response = self.client.post(reverse('finance_onec_refresh_apply_step', args=[run.pk]), {'cursor': run.cursor['version']})
            self.assertEqual(response.status_code, 403)
            response = self.client.get(reverse('finance_onec_import_list'))
            self.assertNotContains(response, 'Проверенные данные применены')

    def test_month_with_only_payments_is_preserved_not_imported(self):
        preview = fixture()
        preview.update(status='missing', rows=0, groups=[], organizations_without_rows=[ORG, OTHER_ORG])
        preview['settlements']['rows'] = 2
        preview['settlements']['groups'][0]['record_type'] = 'Expense'
        preview['settlements']['groups'][0]['recorder_type'] = 'StandardODATA.Document_РасходИзКассы'
        with self.coverage(), patch(PATCH_READER, return_value=preview):
            result = self.step(self.start())
        self.assertEqual(result.progress['outcome'], 'no_change')
        self.assertEqual(result.result_summary[sync.REPORT_PAYROLL]['missing_preserved_months'], [MONTH])
        self.assertFalse(PayrollAccrualMonth.objects.exists())

    def test_missing_accrual_with_control_receipt_is_error_not_empty(self):
        preview = fixture()
        preview.update(status='missing', rows=0, groups=[], organizations_without_rows=[ORG, OTHER_ORG])
        preview['settlements']['rows'] = 2
        with self.coverage(), patch(PATCH_READER, return_value=preview):
            result = self.step(self.start())
        self.assertEqual(result.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertFalse(PayrollAccrualMonth.objects.exists())

    def test_missing_accrual_wrong_payment_currency_is_error(self):
        preview = fixture()
        preview.update(status='missing', rows=0, groups=[], organizations_without_rows=[ORG, OTHER_ORG])
        preview['settlements']['rows'] = 2
        preview['settlements']['groups'][0].update(record_type='Expense', currency_guid=ORG)
        with self.coverage(), patch(PATCH_READER, return_value=preview):
            result = self.step(self.start())
        self.assertEqual(result.status, OneCODataSyncRun.STATUS_FAILED)
        self.assertFalse(PayrollAccrualMonth.objects.exists())
