import json
from io import StringIO
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TransactionTestCase, SimpleTestCase, override_settings
from django.test.utils import CaptureQueriesContext
from pool_service.tests.test_crm_inbox import Fixture
from pool_service.crm_inbox_models import CrmInboxEvent
from scripts.crm_intake_diagnostic_receipt import normalize,PREFIX

@override_settings(COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class IntakeDiagnosticsTests(Fixture,TransactionTestCase):
    reset_sequences = True
    def read(self):
        out=StringIO();call_command('crm_intake_diagnostics',organization=1,stdout=out)
        return normalize(out.getvalue(),'a'*40)

    def test_only_capped_metadata_no_writes_no_source_values(self):
        self.incoming()
        before=list(CrmInboxEvent.objects.values())
        with CaptureQueriesContext(connection) as queries:result=self.read()
        self.assertEqual(result['buckets']['pending']['sample_count'],1)
        self.assertFalse(result['buckets']['pending']['truncated'])
        self.assertEqual(before,list(CrmInboxEvent.objects.values()))
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertNotIn('Synthetic',str(result));self.assertNotIn('synthetic-account',str(result))
        self.assertEqual(result['scheduler_ticks'],'not_recorded')

    def test_receipt_rejects_extra_private_values_and_foreign_scope(self):
        result=self.read();result.pop('expected_sha')
        for change in ({'phone':'private'},{'organization_id':2},{'last_attempt_at':'private'},{'process_enabled':1}):
            with self.assertRaises(ValueError):normalize(PREFIX+json.dumps({**result,**change}),'a'*40)
        with self.assertRaises(CommandError):call_command('crm_intake_diagnostics',organization=2,stdout=StringIO())

    def test_large_backlog_is_explicitly_capped(self):
        for index in range(102):self.incoming(external_message_id=str(index))
        pending=self.read()['buckets']['pending']
        self.assertEqual(pending['sample_count'],100)
        self.assertTrue(pending['truncated'])
        self.assertEqual(pending['due_in_sample'],100)


class ReceiptBlockTests(SimpleTestCase):
    def test_lock_or_release_block_is_not_collected_diagnostics(self):
        for state in ('busy','deploying','release_pending'):
            result=normalize(json.dumps({'status':state,'processed':0}),'a'*40)
            self.assertEqual(result['status'],state)
            self.assertFalse(result['diagnostics_collected'])
        with self.assertRaises(ValueError):normalize(json.dumps({'status':'busy','processed':0,'phone':'private'}),'a'*40)
