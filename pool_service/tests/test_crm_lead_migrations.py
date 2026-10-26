"""Additive lead expansion preserves old data and does not enable intake/access."""
from datetime import date

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class LeadIntakeMigrationTests(TransactionTestCase):
    before = [("pool_service", "0155_crm_live_pilot")]
    after = [("pool_service", "0156_crm_lead_intake")]

    def test_forward_repeat_and_disposable_reverse_preserve_existing_rows(self):
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(self.before)
            old = executor.loader.project_state(self.before).apps
            org = old.get_model("pool_service", "Organization").objects.create(name="Synthetic migration", crm_v2_read=True)
            user = old.get_model("auth", "User").objects.create(username="lead-migration-synthetic")
            access = old.get_model("pool_service", "CrmAccess").objects.create(organization_id=org.pk,
                user_id=user.pk, view_team_deals=True, edit_own_leads=True)
            lead = old.get_model("pool_service", "CrmLead").objects.create(organization_id=org.pk,
                title="Existing synthetic lead", need="Existing need", assignee_id=user.pk)
            task = old.get_model("pool_service", "ServiceTask").objects.create(organization_id=org.pk,
                title="Existing unrelated task", start_date=date(2026, 10, 26))
            sentinel_lead = old.get_model("pool_service", "CrmLead").objects.filter(pk=lead.pk).values().get()
            sentinel_task = old.get_model("pool_service", "ServiceTask").objects.filter(pk=task.pk).values().get()
            executor = MigrationExecutor(connection)
            executor.migrate(self.after)
            current = executor.loader.project_state(self.after).apps
            Lead = current.get_model("pool_service", "CrmLead")
            Grant = current.get_model("pool_service", "CrmAccess")
            Link = current.get_model("pool_service", "CrmLeadTaskLink")
            Thread = current.get_model("pool_service", "CrmLeadThread")
            expanded = Lead.objects.get(pk=lead.pk)
            self.assertEqual(Lead.objects.filter(pk=lead.pk).values(*sentinel_lead).get(), sentinel_lead)
            self.assertIsNone(expanded.origin_lead_id)
            self.assertIsNone(expanded.next_task_id)
            self.assertFalse(expanded.needs_review)
            grant = Grant.objects.get(pk=access.pk)
            self.assertTrue(grant.view_team_deals and grant.edit_own_leads)
            self.assertFalse(grant.view_leads or grant.create_own_leads or grant.take_leads or grant.qualify_own_leads)
            organization = current.get_model("pool_service", "Organization").objects.get(pk=org.pk)
            self.assertTrue(organization.crm_v2_read)
            self.assertFalse(organization.crm_v2_write)
            self.assertFalse(organization.crm_intake_process)
            for model in ("CrmIntakeActivation", "CrmLeadSource", "CrmLeadDealLink", "CrmLeadThread", "CrmLeadTaskLink"):
                self.assertFalse(current.get_model("pool_service", model).objects.exists())
            Link.objects.create(organization_id=org.pk, lead_id=lead.pk, task_id=task.pk, created_by_id=user.pk)
            with self.assertRaises(IntegrityError), transaction.atomic():
                Link.objects.create(organization_id=org.pk, lead_id=lead.pk, task_id=task.pk, created_by_id=user.pk)
            Thread.objects.create(organization_id=org.pk, lead_id=lead.pk, context_key="b" * 64)
            with self.assertRaises(IntegrityError), transaction.atomic():
                Thread.objects.create(organization_id=org.pk, lead_id=lead.pk, context_key="b" * 64)
            MigrationExecutor(connection).migrate(self.after)
            self.assertEqual(Thread.objects.count(), 1)
            self.assertEqual(sentinel_task, current.get_model("pool_service", "ServiceTask").objects.filter(pk=task.pk).values().get())
            # Reverse is verified only in a disposable test: never an operational
            # rollback plan after durable source/qualification data exists.
            MigrationExecutor(connection).migrate(self.before)
            self.assertEqual(sentinel_lead, old.get_model("pool_service", "CrmLead").objects.filter(pk=lead.pk).values().get())
            self.assertEqual(sentinel_task, old.get_model("pool_service", "ServiceTask").objects.filter(pk=task.pk).values().get())
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
