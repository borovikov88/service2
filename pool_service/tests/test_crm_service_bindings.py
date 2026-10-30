from dataclasses import replace
from datetime import date,timedelta
from decimal import Decimal as D
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied,ValidationError
from django.test import TestCase,TransactionTestCase,skipUnlessDBFeature
from django.utils import timezone
from pool_service.models import Organization,OrganizationAccess,Client
from pool_service.client_crm_models import ClientOwnedObject
from pool_service.crm_commercial_models import CrmAccess,CrmDeal,CrmPipeline
from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.reward_models import OneCCustomerIdentity
from pool_service.crm_service_binding_models import ServiceContractBinding,ServiceBoundDocument,ServiceAllocationVersion,ServiceBindingEvent
from pool_service.services.crm_service_bindings import save_allocations
from pool_service.services.crm_document_allocations import Allocation
from pool_service.tests import test_onec_service_document_preview as fixture


class BindingFixture:
    def setUp(self):
        self.org=Organization.objects.create(name='Synthetic binding',crm_v2_read=True,crm_v2_write=True,paid_until=timezone.now()+timedelta(days=30))
        self.actor=get_user_model().objects.create_user('synthetic-binding-owner')
        OrganizationAccess.objects.create(organization=self.org,user=self.actor,role='owner')
        self.grant=CrmAccess.objects.create(organization=self.org,user=self.actor,view_team_deals=True,edit_own_deals=True)
        bootstrap_commercial_settings(self.org.pk)
        self.client_record=Client.objects.create(organization=self.org,name='Synthetic service client')
        self.obj=ClientOwnedObject.objects.create(client=self.client_record,object_type='pool',name='Synthetic object')
        pipeline=CrmPipeline.objects.get(organization=self.org,code='service')
        self.deal=CrmDeal.objects.create(organization=self.org,manager=self.actor,client=self.client_record,pipeline=pipeline,
            stage=pipeline.stages.first(),title='Synthetic service',object_kind='client_object',client_object=self.obj)
        f=fixture.DocumentPreviewTests();f.setUp();self.snapshot=f.read()
        OneCCustomerIdentity.objects.create(organization=self.org,client=self.client_record,onec_customer_id=self.snapshot.client_ref,confirmed_by=self.actor,confirmed_at=timezone.now())
        self.override=self.settings(ONEC_ODATA_BASE_URL=f.config.base_url,ONEC_ODATA_USERNAME="synthetic",ONEC_ODATA_PASSWORD="synthetic",ONEC_ODATA_TARGET_ORGANIZATION_ID=self.org.pk,ONEC_ODATA_ORGANIZATION_GUIDS=(self.snapshot.organization_ref,))
        self.override.enable();self.addCleanup(self.override.disable)

    def save(self,**kwargs):
        snapshot=kwargs.get('snapshot',self.snapshot)
        values=dict(actor=self.actor,organization_id=self.org.pk,deal_id=self.deal.pk,snapshot=snapshot,
            allocations=[Allocation(1,snapshot.lines[0].content_hash,date(2026,1,1),D('0.5'),D('60'))],
            operation_key='allocation-1',expected_revision=0,expected_snapshot_hash=snapshot.snapshot_hash,
            confirm_contract=True,confirm_service=True,reason='Explicit synthetic service period')
        values.update(kwargs);return save_allocations(**values)


class ServiceBindingTests(BindingFixture,TestCase):
    def test_explicit_contract_object_and_partial_allocation_replay(self):
        first=self.save();second=self.save()
        self.assertEqual(first['document_id'],second['document_id']);self.assertTrue(second['replayed'])
        self.assertEqual(ServiceAllocationVersion.objects.count(),1)
        self.assertEqual(ServiceBindingEvent.objects.count(),1)
        self.assertEqual(ServiceContractBinding.objects.get().client_object_id,self.obj.pk)
        self.assertEqual(ServiceAllocationVersion.objects.get().allocations[0]['quantity'],'0.5')
        self.deal.refresh_from_db();self.assertEqual(self.deal.outcome,'open');self.assertIsNone(self.deal.settled_at)

    def test_no_second_credit_and_explicit_revision_history(self):
        self.save()
        with self.assertRaises(ValidationError):self.save(operation_key='allocation-2',expected_revision=1)
        self.save(operation_key='reconcile-1',expected_revision=1,reconcile=True)
        self.assertEqual(ServiceAllocationVersion.objects.count(),2)
        self.assertEqual(ServiceBoundDocument.objects.get().active_version.revision,2)

    def test_changed_source_invalidates_before_replay_and_explicit_reconcile(self):
        self.save();changed=replace(self.snapshot,snapshot_hash='b'*64,observed_at=timezone.now().isoformat())
        result=self.save(snapshot=changed,expected_snapshot_hash=self.snapshot.snapshot_hash)
        self.assertFalse(result['saved']);self.assertEqual(result['status'],'requires_reconciliation')
        self.assertEqual(ServiceAllocationVersion.objects.count(),1)
        with self.assertRaises(ValidationError):self.save(snapshot=changed,expected_revision=2,operation_key='new-allocation')
        self.save(snapshot=changed,expected_revision=2,operation_key='explicit-reconcile',reconcile=True)
        self.assertEqual(ServiceAllocationVersion.objects.count(),2)
        self.assertEqual(ServiceBindingEvent.objects.filter(kind='source_changed').count(),1)

    def test_rights_revoked_blocks_replay(self):
        self.save();self.grant.edit_own_deals=False;self.grant.save()
        with self.assertRaises(PermissionDenied):self.save()
        self.assertEqual(ServiceAllocationVersion.objects.count(),1)

    def test_foreign_customer_org_and_missing_confirmation(self):
        for kwargs in ({'snapshot':replace(self.snapshot,client_ref=fixture.ITEM)},
            {'snapshot':replace(self.snapshot,organization_ref=fixture.ITEM)}, {'confirm_contract':False},
            {'confirm_service':False},{'expected_revision':True},{'operation_key':'source-1-2'}):
            with self.subTest(kwargs=list(kwargs)),self.assertRaises((PermissionDenied,ValidationError)):self.save(**kwargs)
        self.assertFalse(ServiceContractBinding.objects.exists())

    def test_one_contract_cannot_bind_second_deal_or_changed_object(self):
        self.save();self.deal.pk=None;self.deal.save()
        with self.assertRaises(ValidationError):self.save(operation_key='second-deal')
        self.assertEqual(ServiceContractBinding.objects.count(),1)

    def test_future_deleted_correction_and_overallocation(self):
        for snapshot in (replace(self.snapshot,deleted=True),replace(self.snapshot,posted=False),
                replace(self.snapshot,document_date=self.snapshot.document_date.replace(year=2099)),
                replace(self.snapshot,warnings=self.snapshot.warnings+('correction_requires_review',))):
            with self.subTest(snapshot=snapshot.posted),self.assertRaises(ValidationError):self.save(snapshot=snapshot)
        with self.assertRaises(ValidationError):self.save(allocations=[Allocation(1,self.snapshot.lines[0].content_hash,date(2026,1,1),D(2),D(240))])
        self.assertFalse(ServiceAllocationVersion.objects.exists())

    def test_reassignment_or_missing_object_cannot_reuse_contract(self):
        self.save();new=ClientOwnedObject.objects.create(client=self.client_record,object_type='pool')
        self.deal.client_object=new;self.deal.save()
        self.assertEqual(self.save()['status'],'requires_reconciliation')

    def test_other_source_old_observation_and_superseded_replay(self):
        first=self.save()
        with self.assertRaises(PermissionDenied): self.save(snapshot=replace(self.snapshot,source_id='c'*64))
        changed=replace(self.snapshot,snapshot_hash='b'*64,observed_at=timezone.now().isoformat())
        self.save(snapshot=changed)
        with self.assertRaises(ValidationError): self.save()
        self.save(snapshot=changed,expected_revision=2,operation_key='reconciled-version',reconcile=True)
        # Exact old request cannot override the new observation.
        self.assertEqual(ServiceAllocationVersion.objects.count(),2)

    def test_scope_change_invalidates_without_storing_foreign_payload(self):
        self.save()
        changed=replace(self.snapshot,contract_ref=fixture.ITEM,snapshot_hash='d'*64,observed_at=timezone.now().isoformat())
        result=self.save(snapshot=changed)
        self.assertFalse(result['saved'])
        doc=ServiceBoundDocument.objects.get()
        self.assertEqual(doc.status,'requires_reconciliation')
        self.assertEqual(set(doc.current_snapshot),{'invalid_scope','observed_at'})

    def test_replay_same_snapshot_after_reconcile_identifies_original_version(self):
        first=self.save()
        self.save(operation_key='reconcile-version',expected_revision=1,reconcile=True)
        replay=self.save()
        self.assertEqual(replay['version_id'],first['version_id'])
        self.assertFalse(replay['active'])
        self.assertEqual(replay['revision'],1)

    def test_invalid_scope_replay_and_restored_mapping_reconcile(self):
        self.save()
        mapping=OneCCustomerIdentity.objects.get();mapping.confirmed_at=None;mapping.save()
        first=self.save();events=ServiceBindingEvent.objects.count()
        self.assertEqual(self.save()['revision'],first['revision'])
        self.assertEqual(ServiceBindingEvent.objects.count(),events)
        mapping.confirmed_at=timezone.now();mapping.save()
        self.save(operation_key='mapping-restored',expected_revision=first['revision'],reconcile=True)
        doc=ServiceBoundDocument.objects.get()
        self.assertNotIn('invalid_scope',doc.current_snapshot)
        self.assertEqual(doc.current_snapshot,doc.active_version.snapshot)
        self.assertEqual(doc.status,'manually_allocated')


@skipUnlessDBFeature('has_select_for_update')
class ServiceBindingRaceTests(BindingFixture,TransactionTestCase):
    def race(self,keys,deal_ids=None):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import close_old_connections,connections
        barrier=Barrier(2)
        def run(key):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:self.save(operation_key=key,deal_id=(deal_ids or {}).get(key,self.deal.pk));return 'ok'
                except ValidationError:return 'conflict'
            finally:connections['default'].close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures=[executor.submit(run,key) for key in keys]
            return [future.result(timeout=30) for future in futures]

    def test_same_operation_one_version(self):
        self.assertEqual(self.race(['same-key','same-key']),['ok','ok'])
        self.assertEqual(ServiceAllocationVersion.objects.count(),1)

    def test_different_operations_cannot_double_consume(self):
        self.assertCountEqual(self.race(['first-key','second-key']),['ok','conflict'])
        self.assertEqual(ServiceAllocationVersion.objects.count(),1)

    def test_two_deals_cannot_claim_one_contract(self):
        original=self.deal.pk
        self.deal.pk=None;self.deal.save();other=self.deal.pk
        self.assertCountEqual(self.race(['claim-a','claim-b'],{'claim-a':original,'claim-b':other}),['ok','conflict'])
        self.assertEqual(ServiceContractBinding.objects.count(),1)
        self.assertEqual(ServiceAllocationVersion.objects.count(),1)
