"""Real upgrades from both release histories preserve their existing records."""
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from importlib import import_module
from types import SimpleNamespace
from django.utils import timezone


class CrmReleaseMigrationTests(TransactionTestCase):
    release = [('pool_service', '0189_contact_management')]
    production = [('pool_service', '0182_merge_onec_review_crm_contact')]
    prepared = [('pool_service', '0185_crm_failure_stage')]

    def upgrade(self, source):
        executor = MigrationExecutor(connection)
        self.assertEqual(executor.loader.detect_conflicts(), {})
        self.assertEqual(executor.loader.graph.leaf_nodes('pool_service'), self.release)
        # Remove both parallel branches, not just descendants of one leaf.
        executor.migrate([('pool_service', '0171_merge_materials_board')])
        executor = MigrationExecutor(connection); executor.migrate(source)
        old = executor.loader.project_state(source).apps
        Org = old.get_model('pool_service', 'Organization')
        org = Org.objects.create(pk=1, name='Synthetic CRM release', crm_v2_read=True, crm_v2_write=True)
        other = Org.objects.create(pk=2, name='Synthetic excluded organization', crm_v2_read=True, crm_v2_write=True)
        user = old.get_model('auth', 'User').objects.create(username='synthetic-release')
        Client = old.get_model('pool_service', 'Client')
        company = Client.objects.create(organization_id=org.pk, client_type='legal', name='Synthetic company')
        person = Client.objects.create(organization_id=org.pk, client_type='private', name='Synthetic person')
        pipeline = old.get_model('pool_service', 'CrmPipeline').objects.create(
            organization_id=org.pk, code='construction', name='Synthetic pipeline')
        stage = old.get_model('pool_service', 'CrmStage').objects.create(
            pipeline_id=pipeline.pk, code='need', name='Synthetic need', position=12)
        deal = old.get_model('pool_service', 'CrmDeal').objects.create(
            organization_id=org.pk, manager_id=user.pk, pipeline_id=pipeline.pk, stage_id=stage.pk,
            client_id=company.pk, contact_id=person.pk, title='Synthetic historical loss',
            outcome='lost', close_reason='Synthetic refusal', closed_at=timezone.now())
        lead = old.get_model('pool_service', 'CrmLead').objects.create(
            organization_id=org.pk, assignee_id=user.pk, client_id=company.pk, contact_id=person.pk,
            title='Synthetic completed lead', status='closed',
            result='customer_saved' if source == self.prepared else 'qualified')
        grant = old.get_model('pool_service', 'CrmAccess').objects.create(organization_id=org.pk,
            user_id=user.pk, transition_own_deals=True, manage_own_next_action=True)
        objects = [('Organization', org.pk), ('Organization', other.pk), ('CrmAccess', grant.pk), ('Client', company.pk), ('Client', person.pk),
            ('CrmDeal', deal.pk), ('CrmLead', lead.pk)]
        if source == self.production:
            candidate = old.get_model('pool_service', 'ClientImportCandidate').objects.create(
                organization_id=org.pk, source_ref='11111111-2222-4333-8444-555555555555',
                source_kind='private', name='Synthetic reviewed person', matched_client_id=person.pk,
                status='imported', review_state={'source_version': 4}, source_available=True)
            proposal = old.get_model('pool_service', 'ClientImportProposal').objects.create(
                candidate_id=candidate.pk, client_id=person.pk, field='name', source_version=4,
                before='Synthetic person', after='Synthetic reviewed person')
            objects.extend([('ClientImportCandidate', candidate.pk), ('ClientImportProposal', proposal.pk)])
        snapshots = [(name, pk, old.get_model('pool_service', name).objects.filter(pk=pk).values().get())
            for name, pk in objects]
        executor = MigrationExecutor(connection)
        plan = [migration.name for migration, backwards in executor.migration_plan(self.release) if not backwards]
        if source == self.production:
            self.assertEqual(plan, ['0182_lead_customer_completion', '0183_crm_order_finance',
                '0184_crm_assignment_delivery', '0185_crm_failure_stage', '0186_merge_crm_release', '0187_enable_aqualine_crm_close', '0188_crm_card_comments', '0189_contact_management'])
        else:
            self.assertEqual(plan, ['0172_clientimportcandidate_review_state_and_more',
                '0182_merge_onec_review_crm_contact', '0186_merge_crm_release', '0187_enable_aqualine_crm_close', '0188_crm_card_comments', '0189_contact_management'])
        executor.migrate(self.release)
        new = executor.loader.project_state(self.release).apps
        for name, pk, snapshot in snapshots:
            current = new.get_model('pool_service', name).objects.filter(pk=pk).values().get()
            for field, value in snapshot.items():
                if name == 'Organization' and pk == 1 and field == 'crm_close':
                    self.assertTrue(current[field])
                    continue
                self.assertEqual(current[field], value, (name, field))
        self.assertEqual(new.get_model('pool_service', 'DataAuditLog').objects.filter(
            organization_id=1, entity_type='crm_release_activation').count(), 1)
        if source == self.production:
            self.assertEqual(new.get_model('pool_service', 'CrmStage').objects.get(
                pipeline_id=pipeline.pk, code='failed').position, 13)
        self.assertEqual(MigrationExecutor(connection).migration_plan(self.release), [])
        new.get_model('pool_service', 'CrmLead').objects.create(organization_id=org.pk,
            title='Synthetic lost lead', status='closed', result='lost', reason='Synthetic refusal')
        # Both branches' newly introduced tables are available without fake migrations.
        self.assertEqual(new.get_model('pool_service', 'CrmAssignmentDelivery').objects.count(), 0)
        self.assertEqual(new.get_model('pool_service', 'CrmOneCOrder').objects.count(), 0)
        self.assertEqual(new.get_model('pool_service', 'CrmCardComment').objects.count(), 0)

    def restore_release(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_deployed_0182_to_release_preserves_review_and_explicit_contacts(self):
        try:
            self.upgrade(self.production)
        finally:
            self.restore_release()

    def test_prepared_0185_to_release_preserves_completed_leads_and_contacts(self):
        try:
            self.upgrade(self.prepared)
        finally:
            self.restore_release()


class CrmReleaseActivationTests(TestCase):
    def apply(self):
        apps = MigrationExecutor(connection).loader.project_state(CrmReleaseMigrationTests.release).apps
        import_module('pool_service.migrations.0187_enable_aqualine_crm_close').enable_negative_close(
            apps, SimpleNamespace(connection=connection))

    def test_does_not_grant_access_to_disabled_crm_or_other_organizations(self):
        from pool_service.models import Organization, DataAuditLog
        for pk, read, write in ((1, True, False), (2, True, True)):
            Organization.objects.create(pk=pk, name='Synthetic activation scope '+str(pk), crm_v2_read=read, crm_v2_write=write)
        self.apply()
        self.assertFalse(Organization.objects.filter(crm_close=True).exists())
        self.assertEqual(DataAuditLog.objects.filter(entity_type='crm_release_activation').count(), 0)

    def test_activation_is_audited_once_and_does_not_restore_revoked_flag(self):
        from pool_service.models import Organization, DataAuditLog
        org = Organization.objects.create(pk=1, name='Synthetic permitted CRM', crm_v2_read=True, crm_v2_write=True)
        self.apply(); self.apply()
        org.refresh_from_db(); self.assertTrue(org.crm_close)
        self.assertEqual(DataAuditLog.objects.filter(entity_type='crm_release_activation').count(), 1)
        org.crm_close = False; org.save(update_fields=['crm_close'])
        self.apply(); org.refresh_from_db(); self.assertFalse(org.crm_close)

    def test_already_enabled_scope_also_records_activation_without_overriding_later_revocation(self):
        from pool_service.models import Organization, DataAuditLog
        org = Organization.objects.create(pk=1, name='Synthetic existing close',
            crm_v2_read=True, crm_v2_write=True, crm_close=True)
        self.apply()
        audit = DataAuditLog.objects.get(entity_type='crm_release_activation')
        self.assertEqual((audit.before, audit.changed_fields), ({'crm_close': True}, []))
        org.crm_close = False; org.save(update_fields=['crm_close'])
        self.apply(); org.refresh_from_db(); self.assertFalse(org.crm_close)
