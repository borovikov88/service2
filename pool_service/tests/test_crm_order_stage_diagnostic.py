"""No network: fixed stages preserve retryability without exporting source text."""
from unittest.mock import patch
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import SimpleTestCase, TestCase
from pool_service.finance_imports.crm_orders import _stage, CrmOrderSyncStageError, sync_crm_orders
from pool_service.finance_imports.odata_profit import ODataConfig, ODataPreviewError
from pool_service.finance_imports.odata_daily_sync import finalize_crm_orders_step
from pool_service.tests import test_crm_order_sync as fixture

class StageBoundaryTests(SimpleTestCase):
    def test_only_expected_errors_are_wrapped_without_public_cause(self):
        for error in (ValidationError('PRIVATE INPUT'),ODataPreviewError('PRIVATE INPUT')):
            def fail(): raise error
            with self.assertRaises(CrmOrderSyncStageError) as caught: _stage('read',fail)
            self.assertEqual(caught.exception.stage,'read')
            self.assertNotIn('PRIVATE',str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)
            self.assertTrue(caught.exception.__suppress_context__)
        for error in (PermissionDenied('unchanged'),RuntimeError('unchanged')):
            def fail(): raise error
            with self.assertRaises(type(error)) as caught: _stage('apply',fail)
            self.assertIs(caught.exception,error)

class StageIntegrationTests(TestCase):
    def setUp(self): fixture.CrmOrderSyncTests.setUp(self)
    def test_wrapper_locates_config_read_apply_with_mocked_io(self):
        config=ODataConfig(base_url='https://example.test/odata/standard.odata/', username='synthetic',password='synthetic',organization_guids=('11111111-1111-4111-8111-111111111111',))
        for stage in ('config','read','apply'):
            with self.subTest(stage=stage), patch('pool_service.finance_imports.crm_orders.validate_config',return_value=config) as validate, patch('pool_service.finance_imports.odata_crm_orders.read_crm_orders',return_value=object()) as read, patch('pool_service.services.crm_order_finance.apply_crm_order_snapshot',return_value={'orders':0}) as apply:
                {'config':validate,'read':read,'apply':apply}[stage].side_effect=ValidationError('PRIVATE')
                with self.assertRaises(CrmOrderSyncStageError) as caught: sync_crm_orders(self.org,self.actor,config=config)
                self.assertEqual(caught.exception.stage,stage)
                self.assertNotIn('PRIVATE',str(caught.exception))
    def test_finalizer_persists_only_enum_and_clears_after_success(self):
        run=fixture.CrmOrderSyncTests.make_run(self)
        with patch(fixture.MODULE+'.sync_crm_orders',side_effect=CrmOrderSyncStageError('apply')),patch('pool_service.services.crm_order_finance.mark_crm_order_sync_error'):
            self.assertEqual(finalize_crm_orders_step(run),'retryable_error')
        run.refresh_from_db();self.assertEqual(run.progress['crm_orders_error_stage'],'apply')
        with patch(fixture.MODULE+'.sync_crm_orders',return_value={'orders':0}):
            self.assertEqual(finalize_crm_orders_step(run),'completed')
        run.refresh_from_db();self.assertEqual(run.progress['crm_orders_error_stage'],'')
    def test_terminal_states_clear_stale_diagnostic(self):
        for state in ('completed','failed','disabled'):
            run=fixture.CrmOrderSyncTests.make_run(self,progress={'crm_orders_state':state,'crm_orders_error_stage':'PRIVATE'})
            with patch(fixture.MODULE+'.sync_crm_orders') as sync: self.assertEqual(finalize_crm_orders_step(run),state)
            sync.assert_not_called();run.refresh_from_db();self.assertEqual(run.progress['crm_orders_error_stage'],'')
