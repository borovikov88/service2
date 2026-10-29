"""Empty additive schema, physical identity uniqueness and old-row preservation."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from pool_service.tests.test_crm_object_needs import ReviewFixture


class NeedDealMigrationTests(ReviewFixture, TransactionTestCase):
    def test_empty_additive_tables_preserve_sources_flags_and_existing_commands(self):
        self.execute()  # Synthetic existing source/review/grants before the additive schema.
        before = [('pool_service','0158_crm_object_needs')]
        after = [('pool_service','0159_crm_need_deals')]
        try:
            executor = MigrationExecutor(connection); executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            org = apps.get_model('pool_service','Organization').objects.create(name='Need conversion migration')
            client = apps.get_model('pool_service','Client').objects.create(organization_id=org.pk,name='Synthetic')
            pool = apps.get_model('pool_service','Pool').objects.create(organization_id=org.pk,client_id=client.pk)
            reading = apps.get_model('pool_service','WaterReading').objects.create(pool_id=pool.pk,date=timezone.now(),required_materials='Retained source')
            command = apps.get_model('pool_service','CrmCommand').objects.create(organization_id=org.pk,operation_key='old-key',command_type='deal.create',payload_hash='a'*64,result={})
            rows = [('Organization',org.pk),('WaterReading',reading.pk),('CrmCommand',command.pk)]
            snapshots = {name:apps.get_model('pool_service',name).objects.values().get(pk=pk) for name,pk in rows}
            preserved = ('Organization', 'OrganizationAccess', 'Employee', 'CrmAccess',
                'Client', 'Pool', 'WaterReading', 'ServiceTask', 'CrmItem',
                'CrmObjectNeed', 'CrmObjectNeedSourceRevision', 'CrmObjectNeedReview', 'CrmCommand')
            all_rows = {name: list(apps.get_model('pool_service', name).objects.order_by('pk').values())
                for name in preserved}
            executor = MigrationExecutor(connection); executor.migrate(after)
            current = executor.loader.project_state(after).apps
            for name,pk in rows: self.assertEqual(current.get_model('pool_service',name).objects.values().get(pk=pk),snapshots[name])
            for name, expected in all_rows.items():
                self.assertEqual(list(current.get_model('pool_service', name).objects.order_by('pk').values()), expected)
            for name in ('CrmObjectNeedLine','CrmObjectNeedReviewLine','CrmObjectNeedDealLine'):
                self.assertEqual(current.get_model('pool_service',name).objects.count(),0)
            with connection.cursor() as cursor:
                constraints = connection.introspection.get_constraints(cursor,'pool_service_crmobjectneeddealline')
            self.assertTrue(any(value['unique'] and value['columns'] == ['line_id'] for value in constraints.values()))
            self.assertTrue(any(value['unique'] and value['columns'] == ['review_line_id'] for value in constraints.values()))
            with connection.cursor() as cursor:
                bindings = connection.introspection.get_constraints(cursor, 'pool_service_crmobjectneedreviewline')
            for columns in (['review_id', 'line_token'], ['review_id', 'line_id']):
                self.assertTrue(any(value['unique'] and value['columns'] == columns for value in bindings.values()))
        finally:
            executor = MigrationExecutor(connection); executor.migrate(executor.loader.graph.leaf_nodes())
