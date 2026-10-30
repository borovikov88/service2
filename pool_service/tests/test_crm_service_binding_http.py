from dataclasses import replace
from unittest.mock import patch
from django.test import TestCase,Client
from django.urls import reverse
from django.utils import timezone
from pool_service.tests.test_crm_service_bindings import BindingFixture
from pool_service.crm_service_binding_models import ServiceAllocationVersion,ServiceBoundDocument
from pool_service.services import crm_service_binding_reader as reader
from pool_service.onec_diagnostic import OneCDiagnosticError


class BindingHTTPTests(BindingFixture,TestCase):
    def setUp(self):
        super().setUp();self.client.force_login(self.actor)
        self.url=reverse('crm_service_bindings',args=[self.deal.pk])+'?org_id='+str(self.org.pk)
        self.allow=self.settings(ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_USER_IDS=(self.actor.pk,));self.allow.enable();self.addCleanup(self.allow.disable)
        self.refs={key:getattr(self.snapshot,key) for key in ('document_ref','organization_ref','client_ref','contract_ref')}

    def preview(self):return self.client.post(self.url,{'action':'preview',**self.refs})
    def save_form(self,response):
        return dict(action='save',selection=response.context['selection'],line='1',month='2026-01',quantity='0.5',amount='60',
            contract_confirm='yes',service_confirm='yes',reason='Synthetic explicit month')

    def test_server_snapshot_save_repeat_history_and_browser_amount_is_only_allocation(self):
        with patch.object(reader,'read_document',return_value=self.snapshot) as fetch:
            preview=self.preview();self.assertEqual(preview.status_code,200)
            form=self.save_form(preview);form['snapshot']='forged financial content'
            self.assertEqual(self.client.post(self.url,form).status_code,302)
            self.assertEqual(self.client.post(self.url,form).status_code,302)
            self.assertEqual(fetch.call_count,3)
        self.assertEqual(ServiceAllocationVersion.objects.count(),1)
        self.assertContains(self.client.get(self.url),'manually_allocated')

    def test_localized_amount_is_decimal_allocation_not_new_source_value(self):
        with patch.object(reader,'read_document',return_value=self.snapshot):
            form=self.save_form(self.preview());form['amount']='60,00'
            self.assertEqual(self.client.post(self.url,form).status_code,302)
        self.assertEqual(ServiceAllocationVersion.objects.get().allocations[0]['amount'],'60.00')

    def test_changed_source_requires_new_explicit_selection(self):
        with patch.object(reader,'read_document',return_value=self.snapshot): preview=self.preview()
        changed=replace(self.snapshot,snapshot_hash='e'*64,observed_at=timezone.now().isoformat())
        with patch.object(reader,'read_document',return_value=changed):
            response=self.client.post(self.url,self.save_form(preview));self.assertEqual(response.status_code,409)
            self.assertContains(response,'source_changed',status_code=409)
            self.assertFalse(ServiceAllocationVersion.objects.exists())
            self.assertEqual(self.client.post(self.url,self.save_form(response)).status_code,302)

    def test_revoked_access_and_cross_deal_token_do_not_fetch(self):
        with patch.object(reader,'read_document',return_value=self.snapshot):preview=self.preview()
        self.grant.edit_own_deals=False;self.grant.save()
        with patch.object(reader,'read_document') as fetch:
            self.assertEqual(self.client.post(self.url,self.save_form(preview)).status_code,403);fetch.assert_not_called()

    def test_existing_diagnostic_policy_blocks_without_network(self):
        with self.settings(ADVISOR_ONEC_DIAGNOSTIC_MCP_ALLOWED_USER_IDS=()),patch.object(reader,'read_document') as fetch:
            self.assertContains(self.client.get(self.url),'Доступ к чтению 1С заблокирован')
            self.assertEqual(self.preview().status_code,403);fetch.assert_not_called()

    def test_negative_and_unavailable_source_are_explicitly_blocked(self):
        with patch.object(reader,'read_document',return_value=replace(self.snapshot,warnings=('correction_requires_review',))):
            response=self.preview();self.assertContains(response,'Отрицательные корректировки')
            self.assertNotContains(response,'data-allocation-form')
        with patch.object(reader,'read_document',side_effect=OneCDiagnosticError('SYNTHETIC_FAILURE')):
            self.assertEqual(self.preview().status_code,503)
        self.assertFalse(ServiceAllocationVersion.objects.exists())

    def test_csrf_and_tampered_token(self):
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.actor)
        self.assertEqual(csrf.post(self.url,{'action':'preview',**self.refs}).status_code,403)
        with patch.object(reader,'read_document') as fetch:
            self.assertEqual(self.client.post(self.url,{'action':'save','selection':'tampered'}).status_code,409);fetch.assert_not_called()

    def test_reader_reuses_transport_and_checks_customer_before_fetch(self):
        with patch.object(reader.source,'fetch_metadata',return_value=b'synthetic') as metadata,patch.object(reader,'read_service_document_snapshot',return_value=self.snapshot) as adapter:
            result=reader.read_document(actor=self.actor,organization_id=self.org.pk,deal_id=self.deal.pk,**self.refs)
            self.assertEqual(result,self.snapshot);self.assertEqual(metadata.call_count,1)
            self.assertIs(adapter.call_args.kwargs['opener'],metadata.call_args.kwargs['opener'])
        from django.core.exceptions import PermissionDenied
        with patch.object(reader.source,'fetch_metadata') as metadata:
            with self.assertRaises(PermissionDenied):reader.read_document(actor=self.actor,organization_id=self.org.pk,deal_id=self.deal.pk,**{**self.refs,'client_ref':self.snapshot.contract_ref})
            metadata.assert_not_called()

    def test_real_adapter_scope_change_invalidates_without_foreign_payload(self):
        from pool_service.tests import test_onec_service_document_preview as fixture
        self.save()
        f=fixture.DocumentPreviewTests();f.setUp()
        opener=f.reader(header={**f.header,'Договор_Key':fixture.ITEM})
        with patch.object(reader,'build_opener',return_value=opener),patch.object(reader.source,'fetch_metadata',return_value=fixture.metadata()):
            response=self.preview()
        self.assertEqual(response.status_code,409)
        self.assertContains(response,'requires_reconciliation',status_code=409)
        doc=ServiceBoundDocument.objects.get();self.assertEqual(doc.status,'requires_reconciliation')
        self.assertEqual(set(doc.current_snapshot),{'invalid_scope','observed_at'})

    def test_transport_failure_or_wrong_user_refs_cannot_invalidate(self):
        self.save()
        from pool_service.tests import test_onec_service_document_preview as fixture
        f=fixture.DocumentPreviewTests();f.setUp()
        with patch.object(reader.source,'fetch_metadata',side_effect=OneCDiagnosticError('METADATA_HTTP_ERROR')):
            self.assertEqual(self.preview().status_code,503)
        with patch.object(reader,'build_opener',return_value=f.reader()),patch.object(reader.source,'fetch_metadata',return_value=fixture.metadata()):
            self.assertEqual(self.client.post(self.url,{'action':'preview',**self.refs,'contract_ref':fixture.ITEM}).status_code,503)
        self.assertEqual(ServiceBoundDocument.objects.get().status,'manually_allocated')

    def test_revocation_during_read_does_not_render_or_save(self):
        def revoked(**kwargs):
            self.grant.edit_own_deals=False;self.grant.save();return self.snapshot
        with patch.object(reader,'read_document',side_effect=revoked):self.assertEqual(self.preview().status_code,403)
        self.assertFalse(ServiceAllocationVersion.objects.exists())

    def test_reader_rechecks_diagnostic_access_after_transport(self):
        from django.core.exceptions import PermissionDenied
        with patch.object(reader.source,'can_access_diagnostic_mcp',side_effect=[True,False]),patch.object(reader.source,'fetch_metadata',return_value=b'synthetic'),patch.object(reader,'read_service_document_snapshot',return_value=self.snapshot):
            with self.assertRaises(PermissionDenied):reader.read_document(actor=self.actor,organization_id=self.org.pk,deal_id=self.deal.pk,**self.refs)

    def test_expired_token_does_not_fetch(self):
        from django.core import signing
        with patch.object(reader,'read_document',return_value=self.snapshot):preview=self.preview()
        import time
        payload=signing.loads(preview.context['selection'],salt='crm-service-document-selection-v1')
        with patch.object(signing.TimestampSigner,'timestamp',return_value=signing.b62_encode(int(time.time())-901)):
            expired=signing.dumps(payload,salt='crm-service-document-selection-v1')
        form=self.save_form(preview);form['selection']=expired
        with patch.object(reader,'read_document') as fetch:
            self.assertEqual(self.client.post(self.url,form).status_code,409);fetch.assert_not_called()

    def test_token_cannot_be_used_for_another_deal(self):
        with patch.object(reader,'read_document',return_value=self.snapshot):preview=self.preview()
        self.deal.pk=None;self.deal.save()
        other=reverse('crm_service_bindings',args=[self.deal.pk])+'?org_id='+str(self.org.pk)
        with patch.object(reader,'read_document') as fetch:
            self.assertEqual(self.client.post(other,self.save_form(preview)).status_code,403);fetch.assert_not_called()
