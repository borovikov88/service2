from datetime import date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import close_old_connections, connection
from django.test import TestCase,TransactionTestCase,skipUnlessDBFeature
from pool_service.tests.test_crm_handoffs import HandoffFixture
from pool_service.models import Pool,ServiceTask,Client,OrganizationAccess
from pool_service.crm_commercial_models import CrmDeal,CrmAccess
from pool_service.crm_recurring_models import CrmRecurringDraft,CrmRecurringRevision
from pool_service.services.crm_recurring_terms import ServicePeriodDraft as P
from pool_service.services import crm_recurring_drafts as service
from pool_service.services.crm_commercial_commands import CommercialConflict


class DraftFixture(HandoffFixture):
    def setUp(self):
        super().setUp()
        self.deal.refresh_from_db()

    def save(self,**changes):
        args=dict(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
            expected_deal_revision=self.deal.revision,expected_revision=0,operation_key='initial',action='prepare',
            contract_basis='Synthetic existing contract',payer_id=self.customer.pk,reason='Synthetic proposal',
            terms=dict(start=date(2030,1,1),end=date(2030,12,31),complete_period_list=True,
                renewal_review_on=date(2030,11,1),periods=[P(date(2030,1,1),Decimal('0.5'),'мес',None)]))
        args.update(changes);return service.save_draft(**args)

    def renewal(self,**changes):
        args=dict(action='renew',operation_key='renew',expected_revision=1,same_contract_confirmed=True,
            terms=dict(start=date(2031,1,1),end=date(2031,12,31),complete_period_list=True,
                renewal_review_on=date(2031,11,1),periods=[P(date(2031,1,1),Decimal('2'),'шт',Decimal('10.50'))]))
        args.update(changes);return self.save(**args)


class DraftTests(DraftFixture,TestCase):
    def test_prepare_renew_replay_same_contract_no_operational_effect(self):
        before=[list(m.objects.values()) for m in (Pool,ServiceTask,CrmDeal)]
        a=self.save();b=self.renewal();again=self.renewal()
        self.assertEqual(b.pk,again.pk);self.assertEqual(a.draft_id,b.draft_id)
        self.assertEqual(CrmRecurringDraft.objects.count(),1);self.assertEqual(CrmRecurringRevision.objects.count(),2)
        self.assertEqual(a.payload['periods'][0]['agreed_amount'],None)
        self.assertEqual(a.payload['periods'][0]['month'],'2030-01-01')
        self.assertEqual(b.payload['state'],'draft');self.assertEqual(b.payload['operational_effect'],'none')
        self.assertEqual(before,[list(m.objects.values()) for m in (Pool,ServiceTask,CrmDeal)])
        self.assertEqual([r.pk for r in service.read_history(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk)],[b.pk,a.pk])
        a.reason='rewrite'
        with self.assertRaises(ValidationError):a.save()
        with self.assertRaises(ValidationError):CrmRecurringRevision.objects.all().delete()

    def test_revision_conflict_cancel_no_activation_and_rollback(self):
        self.save()
        with self.assertRaises(CommercialConflict):self.save(operation_key='duplicate')
        with self.assertRaises(CommercialConflict):self.save(action='revise',operation_key='stale',expected_revision=0)
        with self.assertRaises(ValidationError):self.save(action='activate',operation_key='activate',expected_revision=1)
        with patch.object(CrmRecurringRevision,'save',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):self.renewal()
        self.assertEqual(CrmRecurringRevision.objects.count(),1)
        self.assertEqual(self.renewal().revision,2)

    def test_renew_requires_same_contract_and_nonoverlapping_explicit_term(self):
        self.save()
        with self.assertRaises(ValidationError):self.renewal(same_contract_confirmed=False)
        with self.assertRaises(CommercialConflict):self.renewal(contract_basis='Different real contract')
        with self.assertRaises(ValidationError):self.save(action='renew',operation_key='overlap',expected_revision=1,same_contract_confirmed=True)
        self.assertEqual(CrmRecurringRevision.objects.count(),1)
        self.assertEqual(CrmRecurringDraft.objects.count(),1)

    def test_revise_only_draft_same_term_preserves_history(self):
        first=self.save()
        revised=self.save(action='revise',operation_key='edit',expected_revision=1,reason='Explicit changed draft')
        self.assertEqual(revised.revision,2);first.refresh_from_db();self.assertEqual(first.reason,'Synthetic proposal')
        with self.assertRaises(ValidationError):self.renewal(action='revise',expected_revision=2)

    def test_rights_revoked_replay_and_foreign_scope(self):
        self.save();grant=CrmAccess.objects.get(user=self.user,organization=self.org)
        grant.edit_own_deals=False;grant.save()
        with self.assertRaises(PermissionDenied):self.save()
        with self.assertRaises(PermissionDenied):service.read_history(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk)

    def test_foreign_payer_other_owner_and_changed_object(self):
        other=Client.objects.create(name='Synthetic out of scope')
        with self.assertRaises(PermissionDenied):self.save(payer_id=other.pk)
        with self.assertRaises(PermissionDenied):self.save(actor=self.recipient)
        self.save()
        new=Pool.objects.create(organization=self.org,client=self.customer,address='Synthetic other object')
        self.deal.pool=new;self.deal.save()
        with self.assertRaises(CommercialConflict):self.renewal()
        with self.assertRaises(PermissionDenied):service.read_history(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk)

    def test_history_reloads_actor_after_superuser_revocation(self):
        self.save()
        self.user.is_superuser=True;self.user.save()
        stale=self.user
        type(self.user).objects.filter(pk=self.user.pk).update(is_superuser=False)
        membership=OrganizationAccess.objects.get(user=self.user,organization=self.org)
        membership.role='accountant';membership.save()
        grant=CrmAccess.objects.get(user=self.user,organization=self.org)
        grant.view_team_deals=True;grant.save()
        with self.assertRaises(PermissionDenied):
            service.read_history(actor=stale,organization_id=self.org.pk,deal_id=self.deal.pk)

    def test_initial_failure_rolls_back_container_and_stale_deal_rejected(self):
        with patch.object(CrmRecurringRevision,'save',side_effect=RuntimeError('synthetic failure')):
            with self.assertRaises(RuntimeError):self.save()
        self.assertFalse(CrmRecurringDraft.objects.exists())
        with self.assertRaises(CommercialConflict):self.save(expected_deal_revision=self.deal.revision+1)
        self.assertFalse(CrmRecurringDraft.objects.exists())

    def test_history_bounded_cursor(self):
        self.save()
        for revision in range(1,22):self.save(action='revise',operation_key=f'edit-{revision}',expected_revision=revision)
        args=dict(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk)
        first=service.read_history(**args);second=service.read_history(**args,before_revision=first[-1].revision)
        self.assertEqual(len(first),20);self.assertEqual([r.revision for r in second],[2,1])


@skipUnlessDBFeature('has_select_for_update')
class DraftRaceTests(DraftFixture,TransactionTestCase):
    def test_same_key_replay_and_competing_revision(self):
        def race(same):
            barrier=Barrier(2)
            def worker(i):
                close_old_connections()
                try:
                    barrier.wait(timeout=10)
                    return self.save(operation_key='initial' if same else f'edit-{i}',
                        action='prepare' if same else 'revise',expected_revision=0 if same else 1).pk
                except CommercialConflict:return 'conflict'
                finally:close_old_connections()
            with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(worker,range(2)))
        first=race(True);self.assertEqual(first[0],first[1]);self.assertEqual(CrmRecurringDraft.objects.count(),1)
        second=race(False);self.assertEqual(second.count('conflict'),1);self.assertEqual(CrmRecurringRevision.objects.count(),2)


class DraftMigrationTests(TransactionTestCase):
    maxDiff = None

    def test_additive_migration_preserves_existing_operational_rows(self):
        from django.db.migrations.executor import MigrationExecutor
        from django.utils import timezone

        executor = MigrationExecutor(connection)
        original_leaves = executor.loader.graph.leaf_nodes()
        source = [('pool_service', '0165_crm_construction_launch')]
        target = [('pool_service', '0166_crm_recurring_drafts')]
        try:
            # The finally covers rollback itself too. Current-model fixtures
            # cannot describe a historical schema (e.g. contact_id arrives in 0181).
            executor.migrate(source)
            apps = executor.loader.project_state(source).apps
            model = lambda name: apps.get_model('pool_service', name)
            user_model = apps.get_model('auth', 'User')
            for number in (1, 2):
                user = user_model.objects.create(username=f'synthetic-migration-{number}',
                    password='!', last_login=timezone.now())
                org = model('Organization').objects.create(name=f'Synthetic migration org {number}')
                customer = model('Client').objects.create(organization=org,
                    name=f'Synthetic migration client {number}', email=f'migration{number}@example.test')
                pool = model('Pool').objects.create(organization=org, client=customer,
                    address=f'Synthetic migration address {number}', volume=number * 10,
                    service_monthly_price=Decimal('100.25') * number)
                pipeline = model('CrmPipeline').objects.create(organization=org,
                    code=f'migration-{number}', name=f'Synthetic pipeline {number}')
                stage = model('CrmStage').objects.create(pipeline=pipeline,
                    code=f'migration-{number}', name=f'Synthetic stage {number}')
                estimate = model('PoolEstimate').objects.create(organization=org,
                    created_by=user, title=f'Synthetic estimate {number}')
                version = model('PoolEstimateVersion').objects.create(estimate=estimate,
                    number=1, snapshot={'synthetic': number}, note=f'Historical version {number}', created_by=user)
                item = model('CrmItem').objects.create(organization=org, client=customer,
                    pool=pool, created_by=user, responsible=user, direction='service',
                    title=f'Synthetic CRM item {number}', amount=Decimal('21.50') * number)
                model('ServiceTask').objects.create(organization=org, client=customer, pool=pool,
                    crm_item=item, created_by=user, primary_responsible=user,
                    title=f'Synthetic task {number}', description=f'Historical task {number}',
                    start_date=date(2030, number, 1), payload_json={'synthetic': number})
                deal = model('CrmDeal').objects.create(organization=org, client=customer, pool=pool,
                    manager=user, pipeline=pipeline, stage=stage, title=f'Synthetic deal {number}',
                    specialization='service', object_kind='pool', currency='RUB', amount_basis='indicative',
                    amount=Decimal('30.75') * number)
                model('CrmQuote').objects.create(organization=org, deal=deal, version=version,
                    created_by=user, variant='economy', source_hash=str(number) * 64,
                    scope={'synthetic': number}, amount=Decimal('41.25') * number)
            names = ('Pool', 'ServiceTask', 'CrmDeal', 'CrmQuote')
            before = {name: list(model(name).objects.order_by('pk').values()) for name in names}
            for rows in before.values():
                self.assertEqual(len(rows), 2)
            executor = MigrationExecutor(connection)
            executor.migrate(target)
            apps = executor.loader.project_state(target).apps
            for name, rows in before.items():
                self.assertEqual(list(apps.get_model('pool_service', name).objects.order_by('pk').values()), rows)
            for name in ('CrmRecurringDraft', 'CrmRecurringRevision'):
                self.assertEqual(apps.get_model('pool_service', name).objects.count(), 0)
        finally:
            MigrationExecutor(connection).migrate(original_leaves)
