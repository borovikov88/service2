from datetime import timedelta,date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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


class MaterialRaceTests(MaterialFixture,TransactionTestCase):
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
