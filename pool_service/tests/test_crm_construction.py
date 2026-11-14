from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from pool_service.tests.test_crm_handoffs import HandoffFixture
from pool_service.models import CrmItem, ServiceTask, OrganizationAccess, Organization
from pool_service.crm_construction_models import CrmConstructionAuthority, CrmConstructionLaunch
from pool_service.crm_commercial_models import CrmEvent, CrmAccess
from pool_service.services import crm_construction as service
from pool_service.services.crm_commercial_commands import CommercialConflict


class ConstructionFixture(HandoffFixture):
    def setUp(self):
        super().setUp()
        self.deal.fulfillment_kind='construction';self.deal.save()
        self.item.direction='project';self.item.stage='prj_design';self.item.save()
        self.authority=CrmConstructionAuthority.objects.create(organization=self.org,user=self.user,enabled=True)
        self.launch_url=reverse('crm_construction',args=[self.deal.pk])+'?org_id='+str(self.org.pk)

    def basis(self,**changes):
        args=dict(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,project_id=self.item.pk,
            task_ids=[self.task.pk],recipient_id=self.recipient.pk,planned_start=timezone.localdate(),
            planned_end=timezone.localdate()+timedelta(days=7),document_note='Synthetic construction documents')
        args.update(changes);return service.preview(**args)

    def launch(self,**changes):
        args=dict(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,basis=None,
            operation_key=uuid4().hex,evidence='Synthetic explicit first launch',confirm_first_launch=True)
        args.update(changes)
        if args['basis'] is None: args['basis']=self.basis()
        return service.launch(**args)


class ConstructionTests(ConstructionFixture,TestCase):
    def test_launch_replay_no_duplicate_or_financial_task_mutation(self):
        basis=self.basis();task=ServiceTask.objects.values().get(pk=self.task.pk)
        deal=type(self.deal).objects.values().get(pk=self.deal.pk)
        first=self.launch(basis=basis,operation_key='once')
        # Avoid fixture helper generating a fresh preview after launch.
        again=service.launch(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
            basis=basis,operation_key='once',evidence='Synthetic explicit first launch',confirm_first_launch=True)
        self.assertEqual(first.pk,again.pk)
        self.assertEqual(CrmConstructionLaunch.objects.count(),1)
        self.assertEqual(CrmEvent.objects.filter(event_type='construction.launched').count(),1)
        self.item.refresh_from_db();self.assertEqual(self.item.stage,'prj_build')
        self.assertEqual(self.item.responsible_id,self.recipient.pk)
        self.assertEqual(CrmItem.objects.count(),1);self.assertEqual(ServiceTask.objects.count(),1)
        self.assertEqual(ServiceTask.objects.values().get(pk=self.task.pk),task)
        self.assertEqual(type(self.deal).objects.values().get(pk=self.deal.pk),deal)
        self.assertEqual(first.basis['version'],self.version.pk)
        first.evidence='tamper'
        with self.assertRaises(ValidationError): first.save()

    def test_changed_execution_is_historical_warning_and_replay_does_not_reset(self):
        basis=self.basis();record=self.launch(basis=basis,operation_key='history')
        self.item.stage='prj_hold';self.item.responsible=self.user;self.item.save()
        self.task.end_date+=timedelta(days=1);self.task.save()
        self.assertTrue(service.visible_record(self.user,self.org,record)['changed'])
        self.launch(basis=basis,operation_key='history')
        self.item.refresh_from_db();self.assertEqual(self.item.stage,'prj_hold')
        self.assertEqual(self.item.responsible_id,self.user.pk)
        self.assertContains(self.http.get(self.launch_url),'изменились после запуска')

    def test_default_denies_even_admin_name_or_superuser(self):
        self.authority.enabled=False;self.authority.save()
        self.user.first_name='Александр';self.user.is_superuser=True;self.user.save()
        OrganizationAccess.objects.filter(organization=self.org,user=self.user).update(role='admin')
        with self.assertRaises(PermissionDenied): self.basis()
        self.assertEqual(CrmConstructionLaunch.objects.count(),0)

    def test_launcher_need_not_be_deal_manager(self):
        self.deal.manager=self.recipient;self.deal.save()
        # Existing team read authority is separate from the personal launch authority.
        self.launch();self.assertEqual(CrmConstructionLaunch.objects.get().actor_id,self.user.pk)

    def test_revoked_authority_source_revision_or_task_change_blocks_commit(self):
        basis=self.basis();self.authority.enabled=False;self.authority.save()
        with self.assertRaises(PermissionDenied): self.launch(basis=basis)
        self.authority.enabled=True;self.authority.save()
        self.task.title='Synthetic changed task';self.task.save()
        with self.assertRaises(CommercialConflict): self.launch(basis=basis)
        basis=self.basis();self.deal.revision+=1;self.deal.save()
        with self.assertRaises(CommercialConflict): self.launch(basis=basis)
        self.assertEqual(CrmConstructionLaunch.objects.count(),0)

    def test_hold_and_existing_launch_never_restart(self):
        for stage in ('prj_hold','prj_build','prj_done'):
            self.item.stage=stage;self.item.save()
            with self.assertRaises(CommercialConflict): self.basis()
        self.item.stage='prj_design';self.item.save();self.launch()
        self.item.stage='prj_design';self.item.save()
        with self.assertRaises(CommercialConflict): self.basis()

    def test_empty_task_project_and_confirm_required(self):
        basis=self.basis(task_ids=[])
        with self.assertRaises(ValidationError): self.launch(basis=basis,confirm_first_launch=False)
        self.launch(basis=basis);self.assertEqual(ServiceTask.objects.count(),1)

    def test_foreign_org_and_recipient_permission_and_private_source(self):
        other=Organization.objects.create(name='Synthetic other organization')
        with self.assertRaises(PermissionDenied): self.basis(organization_id=other.pk)
        OrganizationAccess.objects.filter(organization=self.org,user=self.recipient).delete()
        with self.assertRaises(PermissionDenied): self.basis()

    def test_private_task_and_source_hidden(self):
        from pool_service.communication_models import PhoneCall,TelephonyConnection
        from pool_service.call_processing_models import CallPrivateNumber
        conn=TelephonyConnection.objects.create(organization=self.org,external_id='synthetic-launch')
        call=PhoneCall.objects.create(organization=self.org,connection=conn,source_kind='telephony',external_id='synthetic-launch',employee=self.user,phone_number='+79990000099',started_at=timezone.now())
        self.task.payload_json={'source_call_id':call.pk};self.task.save()
        CallPrivateNumber.objects.create(organization=self.org,owner=self.user,phone_key='9990000099',label='Synthetic private')
        with self.assertRaises(PermissionDenied): self.basis()

    def test_post_launch_private_source_hides_packet_and_denies_replay(self):
        from pool_service.communication_models import PhoneCall,TelephonyConnection
        from pool_service.call_processing_models import CallPrivateNumber
        conn=TelephonyConnection.objects.create(organization=self.org,external_id='synthetic-later-private')
        call=PhoneCall.objects.create(organization=self.org,connection=conn,source_kind='telephony',external_id='synthetic-later-private',employee=self.user,phone_number='+79990000099',started_at=timezone.now())
        self.task.payload_json={'source_call_id':call.pk};self.task.save()
        basis=self.basis();self.launch(basis=basis,operation_key='later-private')
        CallPrivateNumber.objects.create(organization=self.org,owner=self.user,phone_key='9990000099',label='Synthetic private')
        response=self.http.get(self.launch_url)
        self.assertNotContains(response,'data-launch-record')
        self.assertNotContains(response,'Synthetic construction documents')
        with self.assertRaises(PermissionDenied):self.launch(basis=basis,operation_key='later-private')
        self.assertEqual(CrmConstructionLaunch.objects.count(),1)

    def test_http_cancel_confirmation_stale_and_replay(self):
        data=dict(action='preview',project_id=self.item.pk,task_id=[self.task.pk],recipient_id=self.recipient.pk,
            planned_start=timezone.localdate().isoformat(),planned_end=(timezone.localdate()+timedelta(days=7)).isoformat(),document_note='Synthetic note')
        response=self.http.post(self.launch_url,data);self.assertEqual(response.status_code,200)
        token=response.context['preview']['token'];self.http.get(self.launch_url)
        self.assertEqual(CrmConstructionLaunch.objects.count(),0)
        payload=dict(action='launch',selection=token,operation_key='http-once',evidence='Synthetic explicit confirmation')
        self.assertEqual(self.http.post(self.launch_url,payload).status_code,400)
        payload['confirm']='on'
        self.assertEqual(self.http.post(self.launch_url,payload).status_code,302)
        self.assertEqual(self.http.post(self.launch_url,payload).status_code,302)
        self.assertContains(self.http.get(self.launch_url),'Запуск зарегистрирован')
        self.authority.enabled=False;self.authority.save()
        self.assertEqual(self.http.post(self.launch_url,payload).status_code,403)


class ConstructionRaceTests(ConstructionFixture,TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_same_project_competing_launches(self):
        basis=self.basis();barrier=Barrier(2)
        def worker(key):
            close_old_connections()
            try:
                actor=get_user_model().objects.get(pk=self.user.pk);barrier.wait(timeout=10)
                try:
                    record=service.launch(actor=actor,organization_id=self.org.pk,deal_id=self.deal.pk,basis=basis,
                        operation_key=key,evidence='Synthetic race',confirm_first_launch=True)
                    return ('ok',record.pk)
                except CommercialConflict:return ('conflict',None)
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,['race-a','race-b']))
        self.assertEqual(sorted(r[0] for r in results),['conflict','ok'])
        self.assertEqual(CrmConstructionLaunch.objects.count(),1);self.assertEqual(ServiceTask.objects.count(),1)


    @skipUnlessDBFeature('has_select_for_update')
    def test_same_key_concurrent_replay(self):
        basis=self.basis();barrier=Barrier(2)
        def worker(_):
            close_old_connections()
            try:
                actor=get_user_model().objects.get(pk=self.user.pk);barrier.wait(timeout=10)
                return service.launch(actor=actor,organization_id=self.org.pk,deal_id=self.deal.pk,basis=basis,
                    operation_key='same-key',evidence='Synthetic race',confirm_first_launch=True).pk
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(worker,range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(CrmConstructionLaunch.objects.count(),1)

    @skipUnlessDBFeature('has_select_for_update')
    def test_rr_authority_revoked_after_initial_snapshot(self):
        if connection.vendor!='mysql':self.skipTest('Native MySQL REPEATABLE READ required')
        basis=self.basis();observed=Event();changed=Event();original=service.lock_crm_graph
        stale=[]
        def pause(*args,**kwargs):
            CrmConstructionAuthority.objects.get(pk=self.authority.pk)
            observed.set()
            if not changed.wait(15):raise AssertionError('Revocation did not finish')
            stale.append(CrmConstructionAuthority.objects.get(pk=self.authority.pk).enabled)
            return original(*args,**kwargs)
        def worker():
            close_old_connections()
            try:
                connection.ensure_connection()
                with connection.cursor() as c:c.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                with patch.object(service,'lock_crm_graph',side_effect=pause):
                    try:
                        service.launch(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,basis=basis,
                            operation_key='rr-revoke',evidence='Synthetic revoke',confirm_first_launch=True)
                    except PermissionDenied:return 'denied'
                return 'unexpected launch'
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(worker)
            try:
                self.assertTrue(observed.wait(15))
                CrmConstructionAuthority.objects.filter(pk=self.authority.pk).update(enabled=False)
            finally:changed.set()
            self.assertEqual(future.result(timeout=25),'denied')
        self.assertEqual(stale,[True]);self.assertEqual(CrmConstructionLaunch.objects.count(),0)


class ConstructionMigrationTests(ConstructionFixture,TransactionTestCase):
    def test_additive_migration_no_grants_or_operational_changes(self):
        from django.db.migrations.executor import MigrationExecutor
        from pool_service.crm_quote_models import CrmQuote
        self.authority.delete()
        models=(CrmItem,ServiceTask,CrmQuote)
        before={m.__name__:list(m.objects.order_by('pk').values()) for m in models}
        executor=MigrationExecutor(connection);executor.migrate([('pool_service','0164_crm_service_handoff')])
        try:
            executor=MigrationExecutor(connection);target=[('pool_service','0165_crm_construction_launch')]
            executor.migrate(target);apps=executor.loader.project_state(target).apps
            for name,rows in before.items():self.assertEqual(list(apps.get_model('pool_service',name).objects.order_by('pk').values()),rows)
            for name in ('CrmConstructionLaunch','CrmConstructionAuthority'):self.assertEqual(apps.get_model('pool_service',name).objects.count(),0)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
