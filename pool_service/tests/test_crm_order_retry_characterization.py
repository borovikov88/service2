"""A completed finance cursor can still attempt a failing CRM post-step.

Synthetic state only; never contacts 1C or imports production data.
"""
from unittest.mock import patch
from django.core.exceptions import ValidationError
from django.test import TestCase
from pool_service.tests import test_onec_daily_sync as fixture
from pool_service.finance_imports.odata_daily_sync import worker_tick
from pool_service.models import OneCODataSyncRun


class CompletedCrmRetryCharacterizationTests(TestCase):
    def setUp(self):
        fixture.DailyFinanceTests.setUp(self)
        self.org.crm_v2_read = True
        self.org.save(update_fields=['crm_v2_read'])

    def test_zero_report_steps_still_attempts_crm_and_returns_retryable(self):
        run = fixture.DailyFinanceTests.run_record(self,
            status=OneCODataSyncRun.STATUS_COMPLETED,
            sync_scope={'_schedule_slot':'2025-01-01T07'},
            progress={'finance_position_state':'completed',
                      'money_forecast_state':'completed','crm_orders_state':'retryable_error'})
        with patch(fixture.MODULE+'.step_unified_sync') as reports, patch(
            fixture.MODULE+'.finalize_finance_position_step', return_value='completed'), patch(
            fixture.MODULE+'.sync_crm_orders', side_effect=ValidationError('SYNTHETIC PRIVATE SOURCE ERROR')) as orders, patch(
            'pool_service.services.crm_order_finance.mark_crm_order_sync_error') as mark:
            result=worker_tick(now=fixture.NOW)
        reports.assert_not_called()
        orders.assert_called_once()
        mark.assert_called_once()
        self.assertEqual(result, {'state':'retryable_error','run':str(run.pk),'steps':0,
            'finance_position_state':'completed','money_forecast_state':'completed','crm_orders_state':'retryable_error','crm_orders_error_stage':'unknown'})
        run.refresh_from_db()
        self.assertEqual(run.status,OneCODataSyncRun.STATUS_COMPLETED)
        self.assertEqual(run.progress['crm_orders_state'],'retryable_error')
        self.assertNotIn('SYNTHETIC PRIVATE',str(run.progress))
