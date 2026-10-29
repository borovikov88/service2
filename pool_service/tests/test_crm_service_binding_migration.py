from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ServiceBindingMigrationTests(TransactionTestCase):
    def test_additive_migration_preserves_existing_contact_rows(self):
        before=[('pool_service','0161_contact_phones_person_links')]
        after=[('pool_service','0162_service_document_bindings')]
        try:
            executor=MigrationExecutor(connection);executor.migrate(before)
            apps=executor.loader.project_state(before).apps
            client=apps.get_model('pool_service','Client').objects.create(name='Synthetic preserved contact')
            phone=apps.get_model('pool_service','ClientContact').objects.create(client_id=client.pk,kind='phone',value='+79001112233',phone_type='personal')
            saved=apps.get_model('pool_service','ClientContact').objects.values().get(pk=phone.pk)
            executor=MigrationExecutor(connection);executor.migrate(after);apps=executor.loader.project_state(after).apps
            self.assertEqual(apps.get_model('pool_service','ClientContact').objects.values().get(pk=phone.pk),saved)
            for name in ('ServiceContractBinding','ServiceBoundDocument','ServiceAllocationVersion','ServiceBindingEvent'):
                self.assertFalse(apps.get_model('pool_service',name).objects.exists())
            # Only empty synthetic tables are reversed. Production rollback must
            # preserve binding/version history; no reverse with data is tested.
            MigrationExecutor(connection).migrate(before)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
