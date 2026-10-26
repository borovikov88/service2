"""Additive empty schema; no backfill, grants or legacy rewrites."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class ObjectNeedMigrationTests(TransactionTestCase):
    def test_additive_schema_preserves_existing_source_and_flags(self):
        before = [('pool_service', '0157_crm_commercial_card')]
        after = [('pool_service', '0158_crm_object_needs')]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(before)
            apps = executor.loader.project_state(before).apps
            org = apps.get_model('pool_service', 'Organization').objects.create(name='Need migration synthetic')
            client = apps.get_model('pool_service', 'Client').objects.create(organization_id=org.pk, name='Synthetic')
            pool = apps.get_model('pool_service', 'Pool').objects.create(organization_id=org.pk, client_id=client.pk)
            reading = apps.get_model('pool_service', 'WaterReading').objects.create(pool_id=pool.pk, date=timezone.now(), required_materials='Historical composition')
            snapshots = {name: apps.get_model('pool_service', name).objects.values().get(pk=pk) for name,pk in [('Organization', org.pk),('WaterReading',reading.pk)]}
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            current = executor.loader.project_state(after).apps
            for name,pk in [('Organization',org.pk),('WaterReading',reading.pk)]:
                self.assertEqual(current.get_model('pool_service',name).objects.values().get(pk=pk), snapshots[name])
            for name in ('CrmObjectNeed','CrmObjectNeedSourceRevision','CrmObjectNeedReview'):
                self.assertFalse(current.get_model('pool_service',name).objects.exists())
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            self.assertEqual(current.get_model('pool_service','WaterReading').objects.count(), 1)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
