from datetime import timedelta,date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import connections,close_old_connections,connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase,TransactionTestCase,SimpleTestCase,skipUnlessDBFeature
from django.urls import reverse
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture
from pool_service.models import Organization,Pool,Client,OrganizationAccess,Employee,ServiceTask,WaterReading
from pool_service.crm_commercial_models import CrmDeal
from pool_service.crm_material_models import CrmMaterialAccess,CrmMaterialIssue,CrmMaterialMovement
from pool_service.services import crm_materials as svc
from pool_service.services.crm_commercial_commands import CommercialConflict


class MaterialFixture(PilotFixture):
    def setUp(self):
        super().setUp()
        self.org.crm_materials=True;self.org.save(update_fields=['crm_materials'])
        self.access=CrmMaterialAccess.objects.create(organization=self.org,user=self.user,record_own=True,review_team=True)
        self.pool=Pool.objects.create(organization=self.org,client=self.client_record,address='Synthetic object A')
        self.other_pool=Pool.objects.create(organization=self.org,client=self.client_record,address='Synthetic object B')
        self.issue_day=svc.today()-timedelta(days=35)
    def issue(self,**kwargs):
        args=dict(actor=self.user,organization_id=self.org.pk,operation_key='synthetic-issue-01',pool_id=self.pool.pk,item_name='Synthetic chemical',unit='канистра',packaging='10 л',quantity='3',occurred_on=self.issue_day,source='Synthetic office journal')
        args.update(kwargs);return svc.issue(**args)
    def move(self,row,**kwargs):
        args=dict(actor=self.user,organization_id=self.org.pk,issue_id=row.pk,operation_key='synthetic-move-01',expected_revision=row.revision,kind='delivery',quantity='2',occurred_on=svc.today(),source='Synthetic witnessed delivery')
        args.update(kwargs);return svc.move(**args)


class MaterialTests(MaterialFixture,TestCase):
    def test_issue_delivery_return_are_independent_and_no_business_side_effects(self):
        before=(CrmDeal.objects.count(),ServiceTask.objects.count(),WaterReading.objects.count())
        row=self.issue();self.assertEqual(svc.balances(row)['held'],3)
        self.assertEqual(self.issue().pk,row.pk)
        movement=self.move(row);self.assertEqual(self.move(row).pk,movement.pk)
        row.refresh_from_db();self.move(row,operation_key='synthetic-return-01',kind='return',quantity='1')
        self.assertEqual(svc.balances(row),dict(issued=Decimal(3),delivered=Decimal(2),returned=Decimal(1),held=Decimal(0),cancelled=False))
        self.assertEqual(before,(CrmDeal.objects.count(),ServiceTask.objects.count(),WaterReading.objects.count()))
        with self.assertRaises(CommercialConflict):self.issue(quantity='4')
        row.refresh_from_db()
        with self.assertRaises(ValidationError):self.move(row,operation_key='synthetic-excess-01',quantity='1')

    def test_reassignment_dates_sources_and_explicit_correction(self):
        row=self.issue()
        with self.assertRaises(ValidationError):self.move(row,pool_id=self.other_pool.pk)
        original=self.move(row,pool_id=self.other_pool.pk,reason='Synthetic changed destination')
        self.assertEqual(original.occurred_on,svc.today());self.assertNotEqual(original.occurred_on.month,row.occurred_on.month)
        self.assertEqual(original.pool_id,self.other_pool.pk);self.assertEqual(row.pool_id,self.pool.pk)
        row.refresh_from_db()
        corrected=self.move(row,operation_key='synthetic-correct-01',kind='correction',reverses_id=original.pk,reason='Synthetic wrong confirmation')
        self.assertEqual(corrected.reverses_id,original.pk);self.assertEqual(svc.balances(row)['held'],3)
        self.assertEqual(self.move(row,operation_key='synthetic-correct-01',kind='correction',reverses_id=original.pk,reason='Synthetic wrong confirmation').pk,corrected.pk)
        row.refresh_from_db()
        with self.assertRaises(CommercialConflict):self.move(row,operation_key='synthetic-correct-02',kind='correction',reverses_id=original.pk,reason='Cannot twice')
        with self.assertRaises(ValidationError):original.save()
        with self.assertRaises(ValidationError):original.delete()

    def test_changed_recipient_client_hides_history_and_blocks_correction(self):
        row=self.issue();original=self.move(row,pool_id=self.other_pool.pk,reason='Synthetic reassignment')
        other=Client.objects.create(organization=self.org,name='Synthetic replacement client')
        self.other_pool.client=other;self.other_pool.save()
        data=svc.read(actor=self.user,organization_id=self.org.pk,issue_id=row.pk)
        self.assertEqual(data[4],[]);self.assertTrue(data[5])
        row.refresh_from_db()
        with self.assertRaises(CommercialConflict):self.move(row,operation_key='synthetic-wrong-client',kind='correction',reverses_id=original.pk,reason='Not allowed')
        with self.assertRaises(CommercialConflict):self.move(row,operation_key='synthetic-wrong-scope')

    def test_correction_cannot_predate_movement_and_csrf_is_required(self):
        row=self.issue();original=self.move(row);row.refresh_from_db()
        with self.assertRaises(ValidationError):self.move(row,operation_key='synthetic-old-correction',kind='correction',reverses_id=original.pk,reason='Synthetic',occurred_on=svc.today()-timedelta(days=1))
        from django.test import Client as HttpClient
        client=HttpClient(enforce_csrf_checks=True);client.force_login(self.user)
        response=client.post(reverse('crm_materials')+'?org_id='+str(self.org.pk),{})
        self.assertEqual(response.status_code,403)

    def test_void_is_not_physical_return_and_preserves_original(self):
        row=self.issue()
        self.move(row,kind='void',quantity='3',reason='Synthetic mistaken issue')
        data=svc.balances(row);self.assertTrue(data['cancelled']);self.assertEqual(data['held'],0);self.assertEqual(data['returned'],0)
        row.refresh_from_db();self.assertEqual(row.quantity,3)
        with self.assertRaises(CommercialConflict):self.move(row,operation_key='synthetic-after-void')
        with self.assertRaises(ValidationError):CrmMaterialIssue.objects.filter(pk=row.pk).delete()

    def test_explicit_deal_matches_object_and_changed_attribution_needs_reason(self):
        def deal(title):
            return CrmDeal.objects.create(organization=self.org,pipeline=self.pipeline,stage=self.pipeline.stages.first(),manager=self.user,client=self.client_record,pool=self.pool,object_kind='pool',title=title)
        first=deal('Synthetic first contract');second=deal('Synthetic separate contract')
        row=self.issue(deal_id=first.pk)
        with self.assertRaises(ValidationError):self.move(row,deal_id=second.pk)
        movement=self.move(row)
        self.assertEqual(movement.deal_id,first.pk)
        row.refresh_from_db()
        moved=self.move(row,operation_key='synthetic-other-deal',quantity='1',deal_id=second.pk,reason='Synthetic explicit allocation')
        self.assertEqual(moved.deal_id,second.pk)
        self.assertEqual(CrmDeal.objects.count(),2)

    def test_client_merge_preserves_material_history_and_blocks_source_links(self):
        from pool_service.client_merge import merge_clients
        from pool_service.client_crm_models import ClientCRMProfile
        row=self.issue()
        target=Client.objects.create(organization=self.org,name='Synthetic merge target')
        ClientCRMProfile.objects.create(client=target,onec_ref='synthetic-material-target')
        with self.assertRaisesMessage(ValueError,'История материалов'):
            merge_clients(self.client_record.pk,target.pk,organization_id=self.org.pk,actor=self.user)
        self.pool.refresh_from_db();self.assertEqual(self.pool.client_id,self.client_record.pk)
        ClientCRMProfile.objects.update_or_create(client=self.client_record,defaults={'onec_ref':'synthetic-material-existing'})
        source=Client.objects.create(organization=self.org,name='Synthetic empty source')
        merge_clients(source.pk,self.client_record.pk,organization_id=self.org.pk,actor=self.user)
        row.refresh_from_db();self.assertEqual(row.client_id,self.client_record.pk)

    def test_invalid_detail_identifier_never_falls_back_to_creation(self):
        self.client.force_login(self.user)
        payload=dict(operation_key='invalid-detail-post',pool_id=self.pool.pk,item_name='Synthetic invalid route',unit='шт',packaging='1 шт',quantity='1',occurred_on=self.issue_day.isoformat(),source='Synthetic',confirmed='on')
        for invalid in [0,999999999999999999999]:
            url=reverse('crm_material_detail',args=[invalid])+'?org_id='+str(self.org.pk)
            self.assertEqual(self.client.get(url).status_code,404)
            self.assertEqual(self.client.post(url,payload).status_code,404)
        self.assertFalse(CrmMaterialIssue.objects.exists())
        self.assertFalse(CrmMaterialMovement.objects.exists())

    def test_units_are_literal_and_invalid_values_or_future_dates_refused(self):
        for invalid in ['0','-1','NaN','Infinity','1.0001','100000000000']:
            with self.assertRaises(ValidationError):self.issue(quantity=invalid)
        with self.assertRaises(ValidationError):self.issue(occurred_on=svc.today()+timedelta(days=1))
        with self.assertRaises(ValidationError):self.issue(unit='')
        row=self.issue(quantity='0,5');self.assertEqual(row.quantity,Decimal('.5'))
        row.quantity=Decimal(1)
        with self.assertRaises(ValidationError):row.save()

    def test_default_deny_current_grants_and_cross_org_or_deal_scope(self):
        row=self.issue();self.access.delete()
        with self.assertRaises(PermissionDenied):self.issue()
        self.access=CrmMaterialAccess.objects.create(organization=self.org,user=self.user,record_own=True)
        other=Organization.objects.create(name='Synthetic foreign');client=Client.objects.create(organization=other,name='Synthetic foreign client');pool=Pool.objects.create(organization=other,client=client,address='Synthetic hidden')
        with self.assertRaises(PermissionDenied):self.issue(pool_id=pool.pk)
        deal=CrmDeal.objects.create(organization=self.org,pipeline=self.pipeline,stage=self.pipeline.stages.first(),manager=self.user,client=self.client_record,title='Synthetic mismatched')
        with self.assertRaises(PermissionDenied):self.issue(deal_id=deal.pk)
        self.org.crm_materials=False;self.org.save(update_fields=['crm_materials'])
        with self.assertRaises(PermissionDenied):svc.read(actor=self.user,organization_id=self.org.pk)

    def test_own_versus_review_and_actor_inactive(self):
        row=self.issue();other=get_user_model().objects.create_user('material-other')
        OrganizationAccess.objects.create(organization=self.org,user=other,role='service');Employee.objects.create(organization=self.org,user=other,display_name='Synthetic other')
        access=CrmMaterialAccess.objects.create(organization=self.org,user=other,record_own=True)
        self.assertEqual(svc.read(actor=other,organization_id=self.org.pk)[2],[])
        with self.assertRaises(PermissionDenied):svc.read(actor=other,organization_id=self.org.pk,issue_id=row.pk)
        access.review_team=True;access.save()
        self.assertEqual(svc.read(actor=other,organization_id=self.org.pk,issue_id=row.pk)[2].pk,row.pk)
        other.is_active=False;other.save()
        with self.assertRaises(PermissionDenied):svc.read(actor=other,organization_id=self.org.pk)

    def test_http_confirmation_cancel_xss_repeat_and_scope(self):
        self.client.force_login(self.user);url=reverse('crm_materials')+'?org_id='+str(self.org.pk)
        self.assertEqual(self.client.get(url).status_code,200)
        payload=dict(operation_key='http-material-01',pool_id=self.pool.pk,item_name='<script>bad()</script>',unit='шт',packaging='Synthetic pack',quantity='3',occurred_on=self.issue_day.isoformat(),source='Synthetic manual')
        self.assertEqual(self.client.post(url,payload).status_code,400);self.assertFalse(CrmMaterialIssue.objects.exists())
        payload['confirmed']='on';response=self.client.post(url,payload);self.assertEqual(response.status_code,302)
        self.assertEqual(self.client.post(url,payload).url,response.url);self.assertEqual(CrmMaterialIssue.objects.count(),1)
        self.assertContains(self.client.get(response.url),'&lt;script&gt;bad()&lt;/script&gt;')
        row=CrmMaterialIssue.objects.get();move=dict(operation_key='http-movement-01',revision=0,kind='delivery',quantity='2',occurred_on=svc.today().isoformat(),source='Synthetic actual',confirmed='on')
        self.assertEqual(self.client.post(response.url,move).status_code,302)
        self.assertEqual(self.client.post(response.url,dict(move,operation_key='http-stale-02')).status_code,409)
        self.assertEqual(self.client.get(reverse('crm_material_detail',args=[row.pk])+'?org_id=99999').status_code,403)


class MaterialMonthTests(MaterialFixture,TestCase):
    def month(self,value='2026-09',**kwargs):
        return svc.monthly(actor=self.user,organization_id=self.org.pk,month=value,**kwargs)

    def test_actual_month_reassignment_units_and_later_correction(self):
        row=self.issue(occurred_on=date(2026,8,31))
        event=self.move(row,pool_id=self.other_pool.pk,reason='Synthetic reassignment',occurred_on=date(2026,9,1))
        group=self.month()[2][0]
        self.assertIsNone(group['issued']);self.assertEqual(group['quantities'][0]['pool_id'],self.other_pool.pk)
        self.assertEqual(group['quantities'][0]['quantity'],2)
        other=self.issue(operation_key='other-unit-issue',unit='кг',packaging='1 кг',occurred_on=date(2026,9,1))
        self.assertEqual(len(self.month()[2]),2)  # identical text does not merge units/issuances
        row.refresh_from_db();self.move(row,kind='correction',quantity='2',reverses_id=event.pk,reason='Synthetic mistake',occurred_on=date(2026,10,1),operation_key='later-correction')
        group=next(g for g in self.month()[2] if g['issue'].pk==row.pk)
        self.assertEqual(group['quantities'],[]);self.assertEqual(group['events'][0].reversal.kind,'correction')
        self.assertEqual(self.month('2026-10')[2][0]['events'][0].kind,'correction')
        self.assertEqual(self.month('2026-08')[2][0]['issued'],3)

    def test_scope_masking_read_only_and_invalid_month(self):
        row=self.issue(occurred_on=date(2026,9,1));self.move(row,pool_id=self.other_pool.pk,reason='Synthetic reassignment',occurred_on=date(2026,9,2))
        different=Client.objects.create(organization=self.org,name='Synthetic changed recipient')
        self.other_pool.client=different;self.other_pool.save(update_fields=['client'])
        group=self.month()[2][0];self.assertTrue(group['restricted']);self.assertNotIn('events',group);self.assertNotIn('quantities',group)
        self.client.force_login(self.user);url=reverse('crm_materials')+'?org_id='+str(self.org.pk)+'&month=2026-09'
        counts=(CrmMaterialIssue.objects.count(),CrmMaterialMovement.objects.count())
        response=self.client.get(url);self.assertEqual(response.status_code,200);self.assertNotContains(response,'Synthetic witnessed delivery')
        self.assertEqual(self.client.post(url,{'confirmed':'on'}).status_code,405)
        self.assertEqual(counts,(CrmMaterialIssue.objects.count(),CrmMaterialMovement.objects.count()))
        self.assertEqual(self.client.get(url+'&month=2026-10').status_code,400)
        for bad in ['2026-00','2026-13','1-1','9999-12','2026-09<script>']:
            with self.assertRaises(ValidationError):self.month(bad)
        self.assertEqual(self.client.get(url.replace('org_id='+str(self.org.pk),'org_id=999999')).status_code,403)
        self.access.delete();self.assertEqual(self.client.get(url).status_code,403)

    def test_monthly_own_only_cannot_read_team_or_foreign_organization(self):
        row=self.issue(occurred_on=date(2026,9,1))
        actor=get_user_model().objects.create_user('monthly-own-only')
        OrganizationAccess.objects.create(organization=self.org,user=actor,role='service')
        Employee.objects.create(organization=self.org,user=actor,display_name='Synthetic own-only')
        grant=CrmMaterialAccess.objects.create(organization=self.org,user=actor,record_own=True)
        self.assertEqual(svc.monthly(actor=actor,organization_id=self.org.pk,month='2026-09')[2],[])
        self.client.force_login(actor)
        url=reverse('crm_materials')+'?org_id='+str(self.org.pk)+'&month=2026-09'
        response=self.client.get(url);self.assertEqual(response.status_code,200);self.assertNotContains(response,'Synthetic chemical')
        self.assertEqual(self.client.get(reverse('crm_material_detail',args=[row.pk])+'?org_id='+str(self.org.pk)).status_code,403)
        grant.review_team=True;grant.save()
        self.assertContains(self.client.get(url),'Synthetic chemical')
        foreign=Organization.objects.create(name='Synthetic foreign monthly',crm_v2_read=True,crm_materials=True)
        self.assertEqual(self.client.get(url.replace('org_id='+str(self.org.pk),'org_id='+str(foreign.pk))).status_code,403)
        self.org.crm_materials=False;self.org.save(update_fields=['crm_materials'])
        self.assertEqual(self.client.get(url).status_code,403)

    def test_bounded_issue_and_event_pages_no_duplicate_rows(self):
        for n in range(27):self.issue(operation_key='page-issue-'+str(n),occurred_on=date(2026,9,1))
        first=self.month();second=self.month(before=first[3])
        self.assertEqual(len(first[2]),25);self.assertEqual(len(second[2]),2)
        self.assertFalse({g['issue'].pk for g in first[2]} & {g['issue'].pk for g in second[2]})
        row=first[2][0]['issue']
        for n in range(21):
            self.move(row,quantity='.001',occurred_on=date(2026,9,2),operation_key='event-page-'+str(n));row.refresh_from_db()
        group=self.month()[2][0];self.assertEqual(len(group['events']),20);self.assertTrue(group['more_events'])
        self.assertEqual(group['quantities'][0]['quantity'],Decimal('.021'))
        for n in range(21):
            destination=Pool.objects.create(organization=self.org,client=self.client_record,address='Synthetic target '+str(n))
            self.move(row,quantity='.001',pool_id=destination.pk,reason='Synthetic distribution',occurred_on=date(2026,9,2),operation_key='target-page-'+str(n));row.refresh_from_db()
        group=self.month()[2][0];self.assertEqual(len(group['quantities']),20);self.assertTrue(group['more_quantities'])


class MaterialRaceTests(MaterialFixture,TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_historical_object_change_after_snapshot_blocks_write(self):
        row=self.issue()
        other=Pool.objects.create(organization=self.org,client=self.client_record,address='Synthetic destination')
        self.move(row,quantity='1',pool_id=other.pk,reason='Synthetic transfer')
        row.refresh_from_db()
        new_client=Client.objects.create(organization=self.org,name='Synthetic replacement')
        original_visible=svc.visible
        def visible(*args):
            result=original_visible(*args)  # establishes the ordinary-read snapshot
            def change():
                close_old_connections()
                try:
                    obj=Pool.objects.get(pk=other.pk);obj.client=new_client;obj.save(update_fields=['client'])
                finally:connections.close_all()
            with ThreadPoolExecutor(max_workers=1) as executor:executor.submit(change).result(timeout=15)
            return result
        with patch.object(svc,'visible',side_effect=visible):
            with self.assertRaises(CommercialConflict):
                self.move(row,operation_key='snapshot-return-01',kind='return',quantity='1')
        self.assertEqual(row.movements.count(),1)

    @skipUnlessDBFeature('has_select_for_update')
    def test_parallel_issue_retry_and_movement_revision(self):
        barrier=Barrier(2)
        def issue():
            close_old_connections()
            try:barrier.wait(10);return self.issue().pk
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as ex:results=list(ex.map(lambda _:issue(),range(2)))
        self.assertEqual(results[0],results[1]);row=CrmMaterialIssue.objects.get(pk=results[0]);barrier=Barrier(2)
        def move(n):
            close_old_connections()
            try:
                barrier.wait(10)
                try:self.move(row,operation_key='race-movement-'+str(n));return 'ok'
                except CommercialConflict:return 'conflict'
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as ex:results=list(ex.map(move,range(2)))
        self.assertCountEqual(results,['ok','conflict']);self.assertEqual(row.movements.count(),1)


class MaterialMigrationTests(TransactionTestCase):
    def test_additive_empty_tables_preserve_existing_pool_and_flags(self):
        before=[('pool_service','0168_crm_recurring_reminder_receipts')];after=[('pool_service','0170_crm_material_journal')]
        try:
            executor=MigrationExecutor(connection);executor.migrate(before);apps=executor.loader.project_state(before).apps
            org=apps.get_model('pool_service','Organization').objects.create(name='Synthetic preserved')
            client=apps.get_model('pool_service','Client').objects.create(organization_id=org.pk,name='Synthetic preserved client')
            pool=apps.get_model('pool_service','Pool').objects.create(organization_id=org.pk,client_id=client.pk,address='Synthetic preserved object')
            saved=apps.get_model('pool_service','Pool').objects.filter(pk=pool.pk).values().get()
            executor=MigrationExecutor(connection);executor.migrate(after);apps=executor.loader.project_state(after).apps
            self.assertEqual(apps.get_model('pool_service','Pool').objects.filter(pk=pool.pk).values().get(),saved)
            self.assertFalse(apps.get_model('pool_service','Organization').objects.get(pk=org.pk).crm_materials)
            for name in ['CrmMaterialAccess','CrmMaterialIssue','CrmMaterialMovement']:self.assertFalse(apps.get_model('pool_service',name).objects.exists())
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())


class MaterialBrowserSettingsTests(SimpleTestCase):
    def test_standalone_flag_uses_file_backed_sqlite(self):
        import os,subprocess,sys
        env={k:v for k,v in os.environ.items() if not k.endswith('_BROWSER_TEST') and k!='SERVICE2_SQLITE_LIVE_SERVER_TEST'}
        env['CRM_MATERIAL_BROWSER_TEST']='1'
        result=subprocess.run([sys.executable,'-c',"from service_site.test_settings import DATABASES; assert DATABASES['default']['TEST']['NAME'] != ':memory:'"],env=env,capture_output=True,text=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
