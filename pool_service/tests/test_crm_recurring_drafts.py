from datetime import date
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import close_old_connections, connection
from django.test import TestCase,TransactionTestCase,skipUnlessDBFeature
from pool_service.tests.test_crm_handoffs import HandoffFixture
from pool_service.models import Pool,ServiceTask,Client
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
        self.save();CrmAccess.objects.filter(user=self.user,organization=self.org).update(edit_own_deals=False)
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


class DraftMigrationTests(DraftFixture,TransactionTestCase):
    def test_additive_migration_preserves_existing_operational_rows(self):
        from django.db.migrations.executor import MigrationExecutor
        from pool_service.crm_quote_models import CrmQuote
        models=(Pool,ServiceTask,CrmDeal,CrmQuote)
        before={m.__name__:list(m.objects.order_by('pk').values()) for m in models}
        executor=MigrationExecutor(connection);executor.migrate([('pool_service','0165_crm_construction_launch')])
        try:
            executor=MigrationExecutor(connection);target=[('pool_service','0166_crm_recurring_drafts')]
            executor.migrate(target);apps=executor.loader.project_state(target).apps
            for name,rows in before.items():self.assertEqual(list(apps.get_model('pool_service',name).objects.order_by('pk').values()),rows)
            for name in ('CrmRecurringDraft','CrmRecurringRevision'):self.assertEqual(apps.get_model('pool_service',name).objects.count(),0)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
