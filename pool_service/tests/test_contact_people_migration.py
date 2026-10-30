from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ContactPeopleMigrationTests(TransactionTestCase):
    def test_legacy_contacts_preserved_and_no_user_mapping_created(self):
        before=[('pool_service','0160_clientbusinessroleevent_clientbusinessrole')]
        after=[('pool_service','0161_contact_phones_person_links')]
        try:
            executor=MigrationExecutor(connection);executor.migrate(before)
            apps=executor.loader.project_state(before).apps
            person=apps.get_model('pool_service','Client').objects.create(name='Synthetic legacy person',client_type='private')
            phone=apps.get_model('pool_service','ClientContact').objects.create(client_id=person.pk,kind='phone',value='+79001112233',sources=['onec'],is_primary=True)
            saved=apps.get_model('pool_service','ClientContact').objects.values().get(pk=phone.pk)
            executor=MigrationExecutor(connection);executor.migrate(after);apps=executor.loader.project_state(after).apps
            actual=apps.get_model('pool_service','ClientContact').objects.values().get(pk=phone.pk)
            self.assertEqual(actual.pop('phone_type'),'other');self.assertEqual(actual,saved)
            self.assertFalse(apps.get_model('pool_service','OrganizationPersonLink').objects.exists())
            MigrationExecutor(connection).migrate(before)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
