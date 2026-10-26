"""0156 -> 0157 is additive; existing deals, lead qualification and flags stay intact."""
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class CommercialCardMigrationTests(TransactionTestCase):
    def test_existing_rows_defaults_and_physical_shape_constraint(self):
        before = [('pool_service', '0156_crm_lead_intake')]
        after = [('pool_service', '0157_crm_commercial_card')]
        try:
            executor = MigrationExecutor(connection)
            executor.migrate(before)
            old = executor.loader.project_state(before).apps
            org = old.get_model('pool_service', 'Organization').objects.create(name='Card migration synthetic')
            user = old.get_model('auth', 'User').objects.create(username='card-migration-synthetic')
            client = old.get_model('pool_service', 'Client').objects.create(organization_id=org.pk, name='Synthetic client')
            pipeline = old.get_model('pool_service', 'CrmPipeline').objects.create(organization_id=org.pk, code='construction', name='Construction')
            stage = old.get_model('pool_service', 'CrmStage').objects.create(pipeline_id=pipeline.pk, code='need', name='Need')
            deal = old.get_model('pool_service', 'CrmDeal').objects.create(organization_id=org.pk, client_id=client.pk,
                pipeline_id=pipeline.pk, stage_id=stage.pk, manager_id=user.pk, title='Existing qualified deal')
            lead = old.get_model('pool_service', 'CrmLead').objects.create(organization_id=org.pk, client_id=client.pk,
                assignee_id=user.pk, title='Existing qualified lead', status='closed', result='qualified')
            grant = old.get_model('pool_service', 'CrmAccess').objects.create(organization_id=org.pk, user_id=user.pk, edit_own_deals=True)
            snapshots = {name: old.get_model('pool_service', name).objects.get(pk=pk).__dict__.copy()
                for name, pk in [('CrmDeal', deal.pk), ('CrmLead', lead.pk), ('CrmAccess', grant.pk), ('Organization', org.pk)]}
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            apps = executor.loader.project_state(after).apps
            for name, pk in [('CrmDeal', deal.pk), ('CrmLead', lead.pk), ('CrmAccess', grant.pk), ('Organization', org.pk)]:
                current = apps.get_model('pool_service', name).objects.get(pk=pk)
                for field, value in snapshots[name].items():
                    if field != '_state': self.assertEqual(getattr(current, field), value, (name, field))
            Deal = apps.get_model('pool_service', 'CrmDeal')
            row = Deal.objects.get(pk=deal.pk)
            self.assertEqual((row.object_kind, row.pool_id, row.client_object_id, row.work_subject,
                row.planned_object_name, row.planned_object_address, row.amount), ('none', None, None, '', '', '', None))
            self.assertFalse(apps.get_model('pool_service', 'Organization').objects.get(pk=org.pk).crm_close)
            # Historical ORM bypasses Model.clean to exercise the actual DB CHECK.
            with self.assertRaises(IntegrityError), transaction.atomic():
                Deal.objects.filter(pk=deal.pk).update(object_kind='planned')
            Deal.objects.filter(pk=deal.pk).update(object_kind='planned', planned_object_name='Synthetic planned object')
            with self.assertRaises(IntegrityError), transaction.atomic():
                Deal.objects.filter(pk=deal.pk).update(object_kind='none')
            MigrationExecutor(connection).migrate(after)
            self.assertEqual(Deal.objects.get(pk=deal.pk).planned_object_name, 'Synthetic planned object')
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
