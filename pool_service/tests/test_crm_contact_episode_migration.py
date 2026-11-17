"""The episode migration preserves existing leads and their provenance."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ContactEpisodeMigrationTests(TransactionTestCase):
    before = [("pool_service", "0194_call_private_number_removal_boundary")]
    after = [("pool_service", "0196_crm_outbound_intake_boundary")]

    def test_additive_forward_repeat_and_disposable_reverse(self):
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(self.before)
            old = executor.loader.project_state(self.before).apps
            old.get_model("pool_service", "CrmInboxBoundary").objects.filter(key="outbound_calls").delete()
            org = old.get_model("pool_service", "Organization").objects.create(name="Synthetic episode migration")
            Lead = old.get_model("pool_service", "CrmLead")
            client = old.get_model("pool_service", "Client").objects.create(organization_id=org.pk, name="Synthetic contact")
            lead = Lead.objects.create(organization_id=org.pk, client_id=client.pk, contact_id=client.pk,
                title="Synthetic manual name", status="closed", result="no_deal", reason="Synthetic result")
            thread = old.get_model("pool_service", "CrmLeadThread").objects.create(organization_id=org.pk,
                lead_id=lead.pk, context_key="e" * 64)
            sentinel = Lead.objects.filter(pk=lead.pk).values().get()
            marker = None
            for _ in range(2):
                executor = MigrationExecutor(connection)
                executor.migrate(self.after)
                current = executor.loader.project_state(self.after).apps
                NewLead = current.get_model("pool_service", "CrmLead")
                self.assertEqual(NewLead.objects.filter(pk=lead.pk).values(*sentinel).get(), sentinel)
                migrated = NewLead.objects.get(pk=lead.pk)
                self.assertEqual(migrated.identity_origin, "legacy")
                self.assertFalse(migrated.title_is_manual)
                self.assertEqual(current.get_model("pool_service", "CrmLeadThread").objects.get(pk=thread.pk).lead_id, lead.pk)
                organization = current.get_model("pool_service", "Organization").objects.get(pk=org.pk)
                self.assertFalse(organization.crm_intake_process)
                boundary = current.get_model("pool_service", "CrmInboxBoundary").objects.get(key="outbound_calls")
                if marker:
                    self.assertEqual(boundary.captured_at, marker)
                marker = boundary.captured_at
            MigrationExecutor(connection).migrate(self.before)
            self.assertEqual(Lead.objects.filter(pk=lead.pk).values().get(), sentinel)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
