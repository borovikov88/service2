"""Real migration/physical constraints, run unchanged on isolated MySQL 8.4 CI."""
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class CommercialMigrationTests(TransactionTestCase):
    before = [("pool_service", "0150_crm_source_link")]
    after = [("pool_service", "0151_crm_commercial_foundation")]

    def test_expand_repeat_reverse_forward_preserves_legacy_and_db_constraints(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        old = executor.loader.project_state(self.before).apps
        org = old.get_model("pool_service", "Organization").objects.create(name="Migration CRM")
        item = old.get_model("pool_service", "CrmItem").objects.create(organization_id=org.pk, title="Legacy sentinel", direction="project", stage="prj_idea")
        before = old.get_model("pool_service", "CrmItem").objects.filter(pk=item.pk).values().get()
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(self.after)
            apps = executor.loader.project_state(self.after).apps
            Organization = apps.get_model("pool_service", "Organization")
            Pipeline = apps.get_model("pool_service", "CrmPipeline")
            Stage = apps.get_model("pool_service", "CrmStage")
            Lead = apps.get_model("pool_service", "CrmLead")
            Deal = apps.get_model("pool_service", "CrmDeal")
            Event = apps.get_model("pool_service", "CrmEvent")
            Command = apps.get_model("pool_service", "CrmCommand")
            self.assertEqual(before, apps.get_model("pool_service", "CrmItem").objects.filter(pk=item.pk).values().get())
            migrated_org = Organization.objects.get(pk=org.pk)
            self.assertFalse(any(getattr(migrated_org, name) for name in ("crm_v2_read", "crm_v2_write", "crm_intake_process", "crm_materials", "crm_accounting", "crm_close", "tender_mail", "tender_autoselect")))
            self.assertFalse(Pipeline.objects.exists())  # no implicit bootstrap/backfill
            pipeline = Pipeline.objects.create(organization_id=org.pk, code="p", name="P")
            stage = Stage.objects.create(pipeline=pipeline, code="s", name="S")
            lead = Lead.objects.create(organization_id=org.pk, title="Tender", kind="tender")
            deal = Deal.objects.create(organization_id=org.pk, pipeline=pipeline, stage=stage, title="Deal")
            # Historical models deliberately bypass current ORM validation. These
            # checks exercise physical DB constraints and MySQL NULL semantics.
            invalid_creates = (
                lambda: Pipeline.objects.create(organization_id=org.pk, code="p", name="Duplicate"),
                lambda: Stage.objects.create(pipeline=pipeline, code="s", name="Duplicate"),
                lambda: Event.objects.create(organization_id=org.pk, event_type="none"),
                lambda: Event.objects.create(organization_id=org.pk, event_type="both", lead=lead, deal=deal),
                lambda: Lead.objects.create(organization_id=org.pk, title="Invalid exact NULL", budget_kind="exact", budget_min=10, currency="RUB"),
                lambda: Lead.objects.create(organization_id=org.pk, title="Invalid unknown zero", budget_min=0),
                lambda: Lead.objects.create(organization_id=org.pk, title="Invalid range", budget_kind="range", budget_min=20, budget_max=10, currency="RUB"),
                lambda: Deal.objects.create(organization_id=org.pk, pipeline=pipeline, stage=stage, title="Unknown without legacy", outcome="unknown"),
                lambda: Deal.objects.create(organization_id=org.pk, pipeline=pipeline, stage=stage, title="Invalid amount", amount=0),
            )
            for create in invalid_creates:
                with self.assertRaises(IntegrityError), transaction.atomic():
                    create()
            Command.objects.create(organization_id=org.pk, operation_key="op", command_type="future", payload_hash="a" * 64)
            with self.assertRaises(IntegrityError), transaction.atomic():
                Command.objects.create(organization_id=org.pk, operation_key="op", command_type="future", payload_hash="b" * 64)
            # FK is a real physical constraint, not solely Model.clean(). SQLite
            # defers it to the explicit check; MySQL rejects immediately.
            with self.assertRaises(IntegrityError), transaction.atomic():
                Stage.objects.create(pipeline_id=pipeline.pk + 999999, code="bad", name="Bad FK")
                connection.check_constraints(table_names=[Stage._meta.db_table])
            expected = {"crm_pipeline_org_code", "crm_stage_pipeline_code", "crm_event_one_subject", "crm_command_org_operation", "crm_lead_budget_shape"}
            found = set()
            with connection.cursor() as cursor:
                for model in (Pipeline, Stage, Event, Command, Lead):
                    found.update(connection.introspection.get_constraints(cursor, model._meta.db_table))
            self.assertTrue(expected <= found, expected - found)
            # Repeat is no-op. Reverse deliberately drops these synthetic v2
            # rows; it is not a data-preserving production rollback.
            MigrationExecutor(connection).migrate(self.after)
            MigrationExecutor(connection).migrate(self.before)
            self.assertEqual(before, old.get_model("pool_service", "CrmItem").objects.filter(pk=item.pk).values().get())
            MigrationExecutor(connection).migrate(self.after)
            again = MigrationExecutor(connection).loader.project_state(self.after).apps
            self.assertFalse(again.get_model("pool_service", "CrmDeal").objects.exists())
            self.assertEqual(before, again.get_model("pool_service", "CrmItem").objects.filter(pk=item.pk).values().get())
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())


    def test_command_capabilities_expand_existing_rows_without_granting_access(self):
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(self.after)
            old = executor.loader.project_state(self.after).apps
            org = old.get_model("pool_service", "Organization").objects.create(name="Command migration synthetic")
            user = old.get_model("auth", "User").objects.create(username="command-migration-synthetic")
            access = old.get_model("pool_service", "CrmAccess").objects.create(
                organization_id=org.pk, user_id=user.pk, view_team_deals=True)
            lead = old.get_model("pool_service", "CrmLead").objects.create(organization_id=org.pk, title="Preserved")
            event = old.get_model("pool_service", "CrmEvent").objects.create(
                organization_id=org.pk, lead_id=lead.pk, event_type="preserved")
            executor = MigrationExecutor(connection)
            leaves = executor.loader.graph.leaf_nodes()
            executor.migrate(leaves)
            current = executor.loader.project_state(leaves).apps
            persisted = current.get_model("pool_service", "CrmAccess").objects.get(pk=access.pk)
            self.assertTrue(persisted.view_team_deals)
            self.assertTrue(all(not getattr(persisted, name) for name in (
                "edit_own_leads", "transition_own_leads", "edit_own_deals", "transition_own_deals")))
            self.assertEqual(current.get_model("pool_service", "CrmEvent").objects.get(pk=event.pk).reason, "")
            self.assertEqual(current.get_model("pool_service", "CrmLead").objects.get(pk=lead.pk).revision, 0)
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
