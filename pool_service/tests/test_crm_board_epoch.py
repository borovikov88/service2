from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
from unittest.mock import patch
from django.db import connection, connections, close_old_connections
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, RequestFactory, skipUnlessDBFeature
from django.utils import timezone
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture
from pool_service.crm_commercial_models import CrmDeal
from pool_service.crm_live_views import _workspace_data
from pool_service.models import Organization


class BoardEpochConcurrencyTests(PilotFixture,TransactionTestCase):
    @skipUnlessDBFeature('has_select_for_update')
    def test_concurrent_delete_does_not_expand_to_new_organization(self):
        from pool_service.crm_commercial_models import CrmLead
        from django.db.models.query import QuerySet
        first=CrmLead.objects.create(organization=self.org,title='Synthetic delete race')
        other=Organization.objects.create(name='Synthetic other epoch')
        discovered=threading.Event();release=threading.Event()
        original=QuerySet.select_for_update
        def pause(queryset,*args,**kwargs):
            if queryset.model is Organization:
                discovered.set()
                if not release.wait(10):raise RuntimeError('delete discovery timeout')
            return original(queryset,*args,**kwargs)
        def remove():
            close_old_connections()
            try:
                with patch.object(QuerySet,'select_for_update',pause):
                    return CrmLead.objects.filter(title='Synthetic delete race').delete()
            finally:connections.close_all()
        # Writer bypasses the mock but uses the normal model contract.
        with ThreadPoolExecutor(max_workers=1) as executor:
            future=executor.submit(remove)
            try:
                self.assertTrue(discovered.wait(10))
                # The second scope is never in the original candidate set.
                with patch.object(QuerySet,'select_for_update',original):
                    second=CrmLead.objects.create(organization=other,title='Synthetic delete race')
            finally:release.set()
            future.result(timeout=10)
        self.assertFalse(CrmLead.objects.filter(pk=first.pk).exists())
        self.assertTrue(CrmLead.objects.filter(pk=second.pk).exists())

    @skipUnlessDBFeature('has_select_for_update')
    def test_move_waits_for_portion_and_invalidates_next_cursor(self):
        self.org.plan_type=Organization.PLAN_COMPANY_PAID
        self.org.paid_until=timezone.now()+timedelta(days=30);self.org.save()
        source=self.pipeline.stages.get(code='need');target=self.pipeline.stages.get(code='technical')
        rows=[CrmDeal.objects.create(organization=self.org,pipeline=self.pipeline,stage=source,client=self.client_record,
            manager=self.user,title=f'Synthetic race {i}') for i in range(51)]
        reading=threading.Event();release=threading.Event();writing=threading.Event();written=threading.Event()
        from pool_service.crm_live_views import _serialize_deals
        def paused(*args,**kwargs):
            reading.set()
            if not release.wait(10): raise RuntimeError('reader barrier timeout')
            return _serialize_deals(*args,**kwargs)
        def read():
            close_old_connections()
            try:
                request=RequestFactory().get('/',{'pipeline':self.pipeline.pk,'layout':'board'})
                request.user=self.user
                with patch('pool_service.crm_live_views._serialize_deals',paused):return _workspace_data(request,self.org)
            finally:connections.close_all()
        def move():
            close_old_connections()
            try:
                row=CrmDeal.objects.get(pk=rows[-1].pk);row.stage=target;writing.set();row.save();written.set()
            finally:connections.close_all()
        with ThreadPoolExecutor(max_workers=2) as executor:
            reader=executor.submit(read)
            try:
                self.assertTrue(reading.wait(10));writer=executor.submit(move)
                self.assertTrue(writing.wait(10));self.assertFalse(written.wait(.2))
            finally:release.set()
            data=reader.result(timeout=10);writer.result(timeout=10)
        self.assertEqual(len(data['deals']),50)
        self.assertEqual(len({r['id'] for r in data['deals']}),50)
        token=next(w['cursor'] for w in data['column_windows'] if w['stage_id']==source.pk)
        request=RequestFactory().get('/',{'pipeline':self.pipeline.pk,'layout':'board','board_column':source.pk,'cursor':token});request.user=self.user
        from pool_service.services.crm_board_cursor import BoardReset
        with self.assertRaises(BoardReset):_workspace_data(request,self.org)


class BoardEpochMigrationTests(TransactionTestCase):
    def test_additive_epoch_does_not_backfill_or_change_existing_business_rows(self):
        before=[('pool_service','0168_crm_recurring_reminder_receipts')]
        after=[('pool_service','0169_crm_board_epoch')]
        try:
            executor=MigrationExecutor(connection);executor.migrate(before)
            apps=executor.loader.project_state(before).apps
            org=apps.get_model('pool_service','Organization').objects.create(name='Synthetic epoch preservation')
            lead=apps.get_model('pool_service','CrmLead').objects.create(organization_id=org.pk,title='Existing synthetic')
            prior=apps.get_model('pool_service','CrmLead').objects.filter(pk=lead.pk).values().get()
            executor=MigrationExecutor(connection);executor.migrate(after)
            apps=executor.loader.project_state(after).apps
            self.assertEqual(apps.get_model('pool_service','CrmLead').objects.filter(pk=lead.pk).values().get(),prior)
            self.assertFalse(apps.get_model('pool_service','CrmBoardEpoch').objects.exists())
            MigrationExecutor(connection).migrate(after)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())


class StagedMaterialMigrationTests(TransactionTestCase):
    def test_cursor_then_materials_preserves_epoch_and_does_not_enable_access(self):
        before=[('pool_service','0169_crm_board_epoch')]
        after=[('pool_service','0171_merge_materials_board')]
        try:
            executor=MigrationExecutor(connection);executor.migrate(before)
            apps=executor.loader.project_state(before).apps
            org=apps.get_model('pool_service','Organization').objects.create(name='Synthetic staged release')
            apps.get_model('pool_service','CrmBoardEpoch').objects.create(organization_id=org.pk,revision=7)
            executor=MigrationExecutor(connection)
            plan=executor.migration_plan(after)
            self.assertEqual([(m.name,backward) for m,backward in plan],[('0170_crm_material_journal',False),('0171_merge_materials_board',False)])
            executor.migrate(after);apps=executor.loader.project_state(after).apps
            self.assertEqual(apps.get_model('pool_service','CrmBoardEpoch').objects.get(organization_id=org.pk).revision,7)
            self.assertFalse(apps.get_model('pool_service','Organization').objects.get(pk=org.pk).crm_materials)
            for name in ['CrmMaterialAccess','CrmMaterialIssue','CrmMaterialMovement']:
                self.assertFalse(apps.get_model('pool_service',name).objects.exists())
            executor=MigrationExecutor(connection);self.assertEqual(executor.migration_plan(after),[])
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
