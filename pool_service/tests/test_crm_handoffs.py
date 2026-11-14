"""Synthetic existing-task handoff: consent is not execution or payment."""
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from uuid import uuid4
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from pool_service.tests.test_crm_quotes import QuoteFixture
from pool_service.models import Pool, CrmItem, ServiceTask, Organization, OrganizationAccess, Client
from pool_service.crm_handoff_models import CrmHandoff, CrmHandoffTask, CrmHandoffEvent
from pool_service.communication_models import PhoneCall, TelephonyConnection
from pool_service.call_processing_models import CallPrivateNumber
from pool_service.services import crm_handoffs as service
from pool_service.services.crm_commercial_commands import CommercialConflict


class HandoffFixture(QuoteFixture):
    def setUp(self):
        super().setUp()
        self.pool=Pool.objects.create(organization=self.org,client=self.customer,address='Synthetic service object')
        self.deal.object_kind='pool';self.deal.pool=self.pool;self.deal.fulfillment_kind='service';self.deal.save()
        self.quote_id=self.prepare()['quote_id'];self.accept(self.quote_id)
        self.recipient=get_user_model().objects.create_user('synthetic-recipient')
        OrganizationAccess.objects.create(organization=self.org,user=self.recipient,role='manager')
        self.item=CrmItem.objects.create(organization=self.org,client=self.customer,pool=self.pool,
            direction='service',stage='srv_new',title='Synthetic existing request',responsible=self.recipient)
        self.task=ServiceTask.objects.create(organization=self.org,client=self.customer,pool=self.pool,crm_item=self.item,
            title='Synthetic existing task',created_by=self.user,primary_responsible=self.recipient,
            start_date=timezone.localdate(),end_date=timezone.localdate()+timedelta(days=2))
        self.task.responsibles.add(self.recipient)
        self.handoff_url=reverse('crm_handoff',args=[self.deal.pk])+'?org_id='+str(self.org.pk)

    def preview(self):
        return service.preview(actor=self.user,organization=self.org,deal_id=self.deal.pk,item_id=self.item.pk,
            task_ids=[self.task.pk],recipient_id=self.recipient.pk,document_note='Synthetic agreed documents')

    def submit(self,**extra):
        args=dict(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,action='submit',
            operation_key=uuid4().hex,basis=self.preview(),evidence='Synthetic explicit handoff')
        args.update(extra);return service.execute(**args)

    def accept_packet(self,**extra):
        args=dict(actor=self.recipient,organization_id=self.org.pk,deal_id=self.deal.pk,action='accept',
            operation_key=uuid4().hex,expected_revision=1,evidence='Synthetic recipient checked packet')
        args.update(extra);return service.execute(**args)


class HandoffTests(HandoffFixture,TestCase):
    def test_second_page_selection_preserves_page_and_scope(self):
        for index in range(21):
            CrmItem.objects.create(organization=self.org,client=self.customer,pool=self.pool,
                direction='service',stage='srv_new',title=f'Synthetic page request {index}')
        response=self.http.get(self.handoff_url+'&page=2')
        self.assertEqual(response.status_code,200)
        items=response.context['items']
        self.assertEqual(items.number,2)
        self.assertEqual(len(items),2)
        selected=items[0]
        response=self.http.get(self.handoff_url+f'&page=2&item={selected.pk}')
        self.assertEqual(response.context['selected'].pk,selected.pk)
        self.assertEqual(response.context['items'].number,2)
        self.assertContains(response,'name="page" value="2"')
        self.assertEqual(CrmHandoff.objects.count(),0)
        self.task.crm_item=selected;self.task.save()
        response=self.http.post(self.handoff_url,dict(action='preview',page='2',item_id=selected.pk,
            task_id=[self.task.pk],recipient_id=self.recipient.pk,document_note='Synthetic page note'))
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.context['items'].number,2)
        self.assertEqual(response.context['selected'].pk,selected.pk)
        self.assertEqual(CrmHandoff.objects.count(),0)

    def test_submit_accept_replay_preserve_tasks_deal_and_history(self):
        old_task=ServiceTask.objects.filter(pk=self.task.pk).values().get()
        self.deal.refresh_from_db();old_deal=type(self.deal).objects.filter(pk=self.deal.pk).values().get()
        basis=self.preview();first=self.submit(operation_key='submit-once',basis=basis)
        self.assertEqual(self.submit(operation_key='submit-once',basis=basis).pk,first.pk)
        self.assertEqual(CrmHandoffEvent.objects.count(),1)
        self.accept_packet(operation_key='accept-once');self.accept_packet(operation_key='accept-once')
        self.assertEqual(CrmHandoff.objects.get().status,'accepted')
        self.assertEqual(CrmHandoffEvent.objects.count(),2)
        self.assertEqual(ServiceTask.objects.count(),1)
        self.assertEqual(ServiceTask.objects.filter(pk=self.task.pk).values().get(),old_task)
        self.assertEqual(type(self.deal).objects.filter(pk=self.deal.pk).values().get(),old_deal)
        self.assertEqual(CrmHandoffTask.objects.get().task_id,self.task.pk)

    def test_preview_cancel_and_http_confirmation(self):
        response=self.http.get(self.handoff_url+'&item='+str(self.item.pk));self.assertEqual(response.status_code,200)
        response=self.http.post(self.handoff_url,dict(action='preview',item_id=self.item.pk,task_id=[self.task.pk],
            recipient_id=self.recipient.pk,document_note='Synthetic note'))
        self.assertEqual(response.status_code,200);self.assertEqual(CrmHandoff.objects.count(),0)
        token=response.context['preview']['token']
        self.http.get(self.handoff_url);self.assertEqual(CrmHandoff.objects.count(),0)
        data=dict(action='submit',selection=token,operation_key='http-once',evidence='Synthetic confirmation')
        self.assertEqual(self.http.post(self.handoff_url,data).status_code,400)
        data['confirm']='on';self.assertEqual(self.http.post(self.handoff_url,data).status_code,302)
        self.assertEqual(self.http.post(self.handoff_url,data).status_code,302)
        self.assertEqual(CrmHandoff.objects.count(),1)

    def test_changed_task_or_scope_blocks_stale_preview_and_acceptance(self):
        basis=self.preview();self.task.end_date+=timedelta(days=1);self.task.save()
        with self.assertRaises(CommercialConflict):self.submit(basis=basis)
        self.assertEqual(CrmHandoff.objects.count(),0)
        packet=self.submit();self.task.visibility='private';self.task.save()
        with self.assertRaises(CommercialConflict):self.accept_packet()
        packet.refresh_from_db();self.assertEqual(packet.status,'pending')
        self.assertContains(self.http.get(self.handoff_url),'требует сверки')
        self.assertNotContains(self.http.get(self.handoff_url),'Synthetic agreed documents')

    def test_replaced_quote_invalidates_previous_acceptance_without_rewrite(self):
        packet=self.submit();self.accept_packet()
        next_quote=self.prepare('standard')['quote_id'];self.accept(next_quote,replace_quote_id=self.quote_id)
        with self.assertRaises(CommercialConflict):service.current_packet(self.user,self.org,packet)
        packet.refresh_from_db();self.assertEqual(packet.quote_id,self.quote_id)
        self.assertEqual(packet.events.count(),2)
        with self.assertRaises(CommercialConflict):self.submit()

    def test_recipient_and_org_rights_not_granted_by_handoff(self):
        self.submit()
        with self.assertRaises(PermissionDenied):self.accept_packet(actor=self.user)
        self.task.responsibles.clear()
        with self.assertRaises(PermissionDenied):self.accept_packet()
        self.assertNotContains(self.http.get(self.handoff_url),'Synthetic agreed documents')
        self.task.responsibles.add(self.recipient)
        self.recipient.is_active=False;self.recipient.save()
        with self.assertRaises(PermissionDenied):self.accept_packet()
        self.assertEqual(CrmHandoff.objects.get().status,'pending')

    def test_private_source_and_unrelated_task_blocked_without_new_permissions(self):
        connection=TelephonyConnection.objects.create(organization=self.org,external_id='synthetic-handoff')
        call=PhoneCall.objects.create(organization=self.org,connection=connection,source_kind='telephony',
            external_id='synthetic-call',employee=self.user,phone_number='+79990000099',started_at=timezone.now())
        self.task.payload_json={'source_call_id':call.pk};self.task.save()
        self.submit()
        CallPrivateNumber.objects.create(organization=self.org,owner=self.user,phone_key='9990000099',label='Synthetic privacy')
        with self.assertRaises(PermissionDenied):self.accept_packet()
        response=self.http.get(self.handoff_url)
        self.assertNotContains(response,'Synthetic agreed documents')
        self.assertNotContains(response,self.task.title)
        self.assertEqual(CrmHandoff.objects.get().status,'pending')

    def test_unrelated_and_foreign_tasks_are_not_attached(self):
        self.task.crm_item=None;self.task.save()
        with self.assertRaises(PermissionDenied):self.preview()
        self.assertEqual(CrmHandoff.objects.count(),0)
        self.task.crm_item=self.item;self.task.save()
        foreign=Client.objects.create(organization=self.org,name='Synthetic other customer')
        self.pool.client=foreign;self.pool.save()
        with self.assertRaises(PermissionDenied):self.preview()

    def test_document_note_escaped_and_post_scope_rechecked(self):
        data=dict(action='preview',item_id=self.item.pk,task_id=[self.task.pk],recipient_id=self.recipient.pk,
            document_note='<script>synthetic()</script>')
        response=self.http.post(self.handoff_url,data)
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'&lt;script&gt;synthetic()&lt;/script&gt;')
        self.assertNotContains(response,'<script>synthetic()</script>')
        token=response.context['preview']['token']
        self.task.payload_json={'source_call_id':123456};self.task.save()
        response=self.http.post(self.handoff_url,dict(action='submit',selection=token,operation_key='stale-source',
            confirm='on',evidence='Synthetic'))
        self.assertEqual(response.status_code,409);self.assertEqual(CrmHandoff.objects.count(),0)

    def test_other_organization_role_and_foreign_task_are_not_authority(self):
        foreign=Organization.objects.create(name='Synthetic unrelated organization')
        OrganizationAccess.objects.create(organization=foreign,user=self.recipient,role='admin')
        self.submit()
        OrganizationAccess.objects.filter(organization=self.org,user=self.recipient).delete()
        with self.assertRaises(PermissionDenied):self.accept_packet()
        self.assertEqual(CrmHandoff.objects.get().status,'pending')
        response=self.http.get(reverse('crm_handoff',args=[self.deal.pk])+'?org_id='+str(foreign.pk))
        self.assertEqual(response.status_code,403)
        self.task.organization=foreign;self.task.save()
        with self.assertRaises(PermissionDenied):service.current_packet(self.user,self.org,CrmHandoff.objects.get())

    def test_construction_and_contract_identity_not_guessed(self):
        self.deal.fulfillment_kind='construction';self.deal.save()
        with self.assertRaisesMessage(ValidationError,'персональной полномочности'):self.preview()
        self.assertEqual(CrmHandoff.objects.count(),0)

    def test_atomic_failure_decline_and_immutable_evidence(self):
        with patch.object(CrmHandoffEvent.objects,'create',side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):self.submit()
        self.assertEqual(CrmHandoff.objects.count(),0);self.assertEqual(CrmHandoffTask.objects.count(),0)
        packet=self.submit();self.accept_packet(action='decline')
        with self.assertRaises(CommercialConflict):self.accept_packet()
        packet.basis={}
        with self.assertRaises(ValidationError):packet.save()
        with self.assertRaises(ValidationError):CrmHandoffEvent.objects.all().delete()


class HandoffConcurrencyTests(HandoffFixture,TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_competing_distinct_commands_preserve_single_packet_and_decision(self):
        basis=self.preview()
        def race(actions):
            barrier=Barrier(2)
            def worker(pair):
                action,key=pair
                close_old_connections()
                try:
                    barrier.wait(timeout=10)
                    try:
                        if action=='submit':self.submit(operation_key=key,basis=basis)
                        else:self.accept_packet(action=action,operation_key=key)
                        return 'saved'
                    except CommercialConflict:return 'conflict'
                finally:close_old_connections()
            with ThreadPoolExecutor(max_workers=2) as executor:
                return sorted(executor.map(worker,actions))
        self.assertEqual(race([('submit','distinct-submit-a'),('submit','distinct-submit-b')]),['conflict','saved'])
        self.assertEqual(race([('accept','distinct-accept'),('decline','distinct-decline')]),['conflict','saved'])
        self.assertEqual(CrmHandoff.objects.count(),1)
        self.assertEqual(CrmHandoffTask.objects.count(),1)
        self.assertEqual(CrmHandoffEvent.objects.count(),2)
        self.assertEqual(ServiceTask.objects.count(),1)

    def _repeatable_read_change(self, change, exception):
        if connection.vendor!='mysql': self.skipTest('Native MySQL REPEATABLE READ required')
        basis=self.preview();observed=Event();changed=Event();original=service.lock_crm_graph
        original_date=self.task.end_date;stale_dates=[]
        def pause(*args,**kwargs):
            # execute's initial ordinary reads have established its RR snapshot.
            ServiceTask.objects.get(pk=self.task.pk)
            observed.set()
            if not changed.wait(15): raise AssertionError('Concurrent update did not finish')
            stale_dates.append(ServiceTask.objects.get(pk=self.task.pk).end_date)
            return original(*args,**kwargs)
        def worker():
            close_old_connections()
            try:
                connection.ensure_connection()
                with connection.cursor() as cursor:cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                with patch.object(service,'lock_crm_graph',side_effect=pause):
                    try:self.submit(basis=basis,operation_key='rr-change')
                    except (PermissionDenied,ValidationError) as exc:return type(exc)
                return None
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future=executor.submit(worker)
            try:
                self.assertTrue(observed.wait(15))
                change()
            finally:changed.set()
            self.assertEqual(future.result(timeout=25),exception)
        self.assertEqual(stale_dates,[original_date])
        self.assertEqual(CrmHandoff.objects.count(),0)

    @skipUnlessDBFeature('has_select_for_update')
    def test_rr_changed_task_after_snapshot_is_rejected(self):
        self._repeatable_read_change(lambda:ServiceTask.objects.filter(pk=self.task.pk).update(
            end_date=self.task.end_date+timedelta(days=1)),CommercialConflict)

    @skipUnlessDBFeature('has_select_for_update')
    def test_rr_revoked_recipient_role_after_snapshot_is_rejected(self):
        self._repeatable_read_change(lambda:OrganizationAccess.objects.filter(
            organization=self.org,user=self.recipient).delete(),PermissionDenied)

    @skipUnlessDBFeature('has_select_for_update')
    def test_rr_removed_task_participant_after_snapshot_is_rejected(self):
        self._repeatable_read_change(lambda:self.task.responsibles.remove(self.recipient),PermissionDenied)

    @skipUnlessDBFeature('has_select_for_update')
    def test_simultaneous_submit_then_accept_is_one_packet_and_decision(self):
        basis=self.preview();barrier=Barrier(2)
        def worker(action):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                if action=='submit':return self.submit(operation_key='race-submit',basis=basis).pk
                return self.accept_packet(operation_key='race-accept').pk
            finally:close_old_connections()
        for action in ('submit','accept'):
            with ThreadPoolExecutor(max_workers=2) as executor:
                values=list(executor.map(worker,[action,action]))
            self.assertEqual(values[0],values[1])
        self.assertEqual(CrmHandoff.objects.count(),1)
        self.assertEqual(CrmHandoffEvent.objects.count(),2)
        self.assertEqual(ServiceTask.objects.count(),1)
