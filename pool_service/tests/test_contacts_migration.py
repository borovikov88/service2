"""Only synthetic test DB; no production reverse/restore operation."""
from datetime import date
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ContactsMigrationTests(TransactionTestCase):
    def test_additive_schema_preserves_client_profile_pool_and_empty_roles(self):
        before=[("pool_service","0159_crm_need_deals")]
        after=[("pool_service","0160_clientbusinessroleevent_clientbusinessrole")]
        try:
            ex=MigrationExecutor(connection);ex.migrate(before)
            apps=ex.loader.project_state(before).apps
            org=apps.get_model("pool_service","Organization").objects.create(name="Synthetic migration")
            client=apps.get_model("pool_service","Client").objects.create(name="Synthetic existing",organization_id=org.pk,client_type="legal")
            profile=apps.get_model("pool_service","ClientCRMProfile").objects.create(client_id=client.pk,onec_ref="00000000-0000-0000-0000-000000000456")
            pool=apps.get_model("pool_service","Pool").objects.create(client_id=client.pk,organization_id=org.pk,address="Synthetic object")
            user=apps.get_model("auth","User").objects.create(username="synthetic-preservation-owner")
            task=apps.get_model("pool_service","ServiceTask").objects.create(client_id=client.pk,organization_id=org.pk,pool_id=pool.pk,title="Synthetic task",start_date=date(2026,1,1),created_by_id=user.pk)
            item=apps.get_model("pool_service","CrmItem").objects.create(client_id=client.pk,organization_id=org.pk,pool_id=pool.pk,title="Synthetic legacy deal",created_by_id=user.pk)
            saved={name:apps.get_model("pool_service",name).objects.get(pk=obj.pk).__dict__.copy() for name,obj in [("Client",client),("ClientCRMProfile",profile),("Pool",pool),("ServiceTask",task),("CrmItem",item)]}
            for row in saved.values():row.pop("_state")
            ex=MigrationExecutor(connection);ex.migrate(after);new=ex.loader.project_state(after).apps
            for name,obj in [("Client",client),("ClientCRMProfile",profile),("Pool",pool),("ServiceTask",task),("CrmItem",item)]:
                self.assertEqual(new.get_model("pool_service",name).objects.values().get(pk=obj.pk),saved[name])
            self.assertFalse(new.get_model("pool_service","ClientBusinessRole").objects.exists())
            self.assertFalse(new.get_model("pool_service","ClientBusinessRoleEvent").objects.exists())
            # Empty-schema reversal only. After real role records, rollback is
            # application-only: retain role tables and history, never reverse.
            MigrationExecutor(connection).migrate(before)
            self.assertEqual(apps.get_model("pool_service","Client").objects.values().get(pk=client.pk),saved["Client"])
            MigrationExecutor(connection).migrate(after)
        finally:
            ex=MigrationExecutor(connection);ex.migrate(ex.loader.graph.leaf_nodes())
