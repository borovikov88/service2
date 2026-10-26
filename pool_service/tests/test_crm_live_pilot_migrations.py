"""The additive pilot schema preserves existing rows and grants no access."""
from datetime import date

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class LivePilotMigrationTests(TransactionTestCase):
    before = [("pool_service", "0154_crm_inbox")]
    after = [("pool_service", "0155_crm_live_pilot")]

    def test_expand_repeat_and_disposable_reverse_preserve_existing_data(self):
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(self.before)
            old = executor.loader.project_state(self.before).apps
            org = old.get_model("pool_service", "Organization").objects.create(name="Pilot migration synthetic")
            user = old.get_model("auth", "User").objects.create(username="pilot-migration-synthetic")
            client = old.get_model("pool_service", "Client").objects.create(organization_id=org.pk, name="Synthetic client")
            grant = old.get_model("pool_service", "CrmAccess").objects.create(
                organization_id=org.pk, user_id=user.pk, view_team_deals=True, edit_own_deals=True)
            pipeline = old.get_model("pool_service", "CrmPipeline").objects.create(
                organization_id=org.pk, code="construction", name="Construction")
            stage = old.get_model("pool_service", "CrmStage").objects.create(pipeline_id=pipeline.pk, code="need", name="Need")
            deal = old.get_model("pool_service", "CrmDeal").objects.create(organization_id=org.pk,
                client_id=client.pk, manager_id=user.pk, pipeline_id=pipeline.pk, stage_id=stage.pk, title="Existing deal")
            task = old.get_model("pool_service", "ServiceTask").objects.create(organization_id=org.pk,
                client_id=client.pk, created_by_id=user.pk, title="Existing task", start_date=date(2026, 10, 26))
            sentinel_deal = old.get_model("pool_service", "CrmDeal").objects.filter(pk=deal.pk).values().get()
            sentinel_task = old.get_model("pool_service", "ServiceTask").objects.filter(pk=task.pk).values().get()
            executor = MigrationExecutor(connection)
            executor.migrate(self.after)
            current = executor.loader.project_state(self.after).apps
            access = current.get_model("pool_service", "CrmAccess").objects.get(pk=grant.pk)
            self.assertTrue(access.view_team_deals and access.edit_own_deals)
            self.assertFalse(access.create_own_deals or access.manage_own_next_action)
            current_org = current.get_model("pool_service", "Organization").objects.get(pk=org.pk)
            self.assertFalse(current_org.crm_v2_read or current_org.crm_v2_write)
            Link = current.get_model("pool_service", "CrmDealTaskLink")
            Pointer = current.get_model("pool_service", "CrmDealNextAction")
            self.assertFalse(Link.objects.exists())
            self.assertFalse(Pointer.objects.exists())
            self.assertEqual(sentinel_deal, current.get_model("pool_service", "CrmDeal").objects.filter(pk=deal.pk).values().get())
            self.assertEqual(sentinel_task, current.get_model("pool_service", "ServiceTask").objects.filter(pk=task.pk).values().get())
            link = Link.objects.create(organization_id=org.pk, deal_id=deal.pk, task_id=task.pk, created_by_id=user.pk)
            pointer = Pointer.objects.create(organization_id=org.pk, deal_id=deal.pk, task_link_id=link.pk)
            # These historical models deliberately exercise physical SQL unique
            # constraints, including on MySQL rather than only Model.clean().
            with self.assertRaises(IntegrityError), transaction.atomic():
                Link.objects.create(organization_id=org.pk, deal_id=deal.pk, task_id=task.pk, created_by_id=user.pk)
            with self.assertRaises(IntegrityError), transaction.atomic():
                Pointer.objects.create(organization_id=org.pk, deal_id=deal.pk, task_link_id=link.pk)
            MigrationExecutor(connection).migrate(self.after)
            self.assertEqual(Pointer.objects.get(pk=pointer.pk).task_link_id, link.pk)
            # Reverse loses new provenance; this is only a disposable test, not
            # an operational rollback plan once real task links exist.
            MigrationExecutor(connection).migrate(self.before)
            self.assertEqual(sentinel_task, old.get_model("pool_service", "ServiceTask").objects.filter(pk=task.pk).values().get())
            MigrationExecutor(connection).migrate(self.after)
            self.assertFalse(Link.objects.exists())
            self.assertEqual(sentinel_deal, current.get_model("pool_service", "CrmDeal").objects.filter(pk=deal.pk).values().get())
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
