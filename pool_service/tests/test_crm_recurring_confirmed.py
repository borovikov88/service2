from datetime import date,timedelta
from decimal import Decimal
from unittest.mock import patch
from django.core.exceptions import ValidationError,PermissionDenied
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from pool_service.tests.test_crm_recurring_drafts import DraftFixture
from pool_service.models import Pool
from pool_service.crm_recurring_models import CrmRecurringConfirmation
from pool_service.services import crm_recurring_drafts as service
from pool_service.services.crm_recurring_terms import ServicePeriodDraft as P
from pool_service.services.crm_recurring_effective import terms_as_of,due_dates


class ConfirmedFixture(DraftFixture):
    def prepare_confirmed(self):
        self.day=date(2030,1,1)
        row=self.save(schedule=dict(frequency='weekly',first_visit=self.day))
        with patch.object(timezone,'localdate',return_value=self.day):
            record=service.confirm(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
                revision_id=row.pk,expected_revision=row.revision,effective_from=self.day,confirm_effect=True)
        return row,record


class ConfirmedTests(ConfirmedFixture,TestCase):
    def test_confirm_replay_and_unknown_price_no_legacy_fallback(self):
        row,record=self.prepare_confirmed()
        with patch.object(timezone,'localdate',return_value=self.day):
            same=service.confirm(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
                revision_id=row.pk,expected_revision=1,effective_from=self.day,confirm_effect=True)
        self.assertEqual(record.pk,same.pk);self.assertEqual(CrmRecurringConfirmation.objects.count(),1)
        state=terms_as_of(self.pool,self.day);self.assertIsNone(state['price']);self.assertEqual(state['frequency'],'weekly')
        self.assertEqual(due_dates(self.pool,self.day,self.day+timedelta(days=20)),[self.day,self.day+timedelta(days=7),self.day+timedelta(days=14)])
        self.assertEqual(terms_as_of(self.pool,date(2030,2,1))['state'],'outside_period')

    def test_all_orm_write_paths_guard_but_unrelated_fields_remain(self):
        self.prepare_confirmed()
        self.pool.service_monthly_price=Decimal('999')
        with self.assertRaises(ValidationError):self.pool.save()
        with self.assertRaises(ValidationError):Pool.objects.filter(pk=self.pool.pk).update(service_frequency='monthly')
        with self.assertRaises(ValidationError):Pool._base_manager.filter(pk=self.pool.pk).update(service_interval_days=5)
        with self.assertRaises(ValidationError):Pool.objects.bulk_update([self.pool],['service_monthly_price'])
        self.pool.refresh_from_db();self.pool.address='Synthetic changed address';self.pool.save(update_fields=['address'])
        Pool.objects.filter(pk=self.pool.pk).update(service_details_comment='Allowed independent comment')
        self.pool.refresh_from_db();self.assertEqual(self.pool.address,'Synthetic changed address')

    def test_model_form_and_legacy_behavior(self):
        from pool_service.pool_details_forms import PoolServiceDetailsForm
        Pool.objects.filter(pk=self.pool.pk).update(service_frequency='monthly')
        self.pool.refresh_from_db();self.assertEqual(self.pool.service_frequency,'monthly')
        self.prepare_confirmed()
        form=PoolServiceDetailsForm(dict(service_frequency='weekly',service_monthly_price='200',service_details_comment=''),instance=self.pool)
        self.assertTrue(form.is_valid(),form.errors)
        form.save();self.pool.refresh_from_db()
        self.assertEqual(self.pool.service_frequency,'monthly')
        self.assertTrue(form.fields['service_frequency'].disabled)

    def test_dated_revision_no_rewrite_and_late_source_access(self):
        first,_=self.prepare_confirmed()
        terms=dict(start=date(2030,1,1),end=date(2030,12,31),complete_period_list=True,renewal_review_on=date(2030,11,1),
            periods=[P(date(2030,1,1),Decimal('.5'),'мес',None),P(date(2030,2,1),Decimal(1),'мес',Decimal(10))])
        second=self.save(action='revise',expected_revision=1,operation_key='next',terms=terms,schedule=dict(frequency='monthly',first_visit=date(2030,2,4)))
        with patch.object(timezone,'localdate',return_value=date(2030,1,2)):
            service.confirm(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
                revision_id=second.pk,expected_revision=2,effective_from=date(2030,2,1),confirm_effect=True)
        self.assertEqual(terms_as_of(self.pool,date(2030,1,20))['frequency'],'weekly')
        self.assertEqual(terms_as_of(self.pool,date(2030,2,1))['price'],Decimal(10))
        self.assertEqual(terms_as_of(self.pool,date(2030,2,1))['frequency'],'monthly')
        first.refresh_from_db();self.assertIsNone(first.payload['periods'][0]['agreed_amount'])

    def test_ui_get_post_confirmation_cancel_and_replay(self):
        url=reverse('crm_recurring',args=[self.deal.pk])+'?org_id='+str(self.org.pk)
        response=self.http.get(url);self.assertEqual(response.status_code,200)
        data=dict(action='prepare',operation_key='http',deal_revision=self.deal.revision,expected_revision=0,
            contract_basis='Synthetic existing contract',payer_id=self.customer.pk,reason='Synthetic explicit terms',
            start='2030-01-01',end='2030-12-31',review_on='2030-11-01',complete='on',month=['2030-01'],
            quantity=['0.5'],unit=['мес'],amount=[''],frequency='weekly',first_visit='2030-01-01')
        self.assertEqual(self.http.post(url,data).status_code,302)
        self.assertEqual(self.http.post(url,data).status_code,302)
        page=self.http.get(url);self.assertContains(page,'Черновик');self.assertContains(page,'Неизвестна')
        self.assertEqual(CrmRecurringConfirmation.objects.count(),0)
        row=service.read_history(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk)[0]
        confirm=dict(action='confirm',revision_id=row.pk,expected_revision=1,effective_from='2030-01-01',confirm_effect='on')
        with patch.object(timezone,'localdate',return_value=date(2030,1,1)):
            self.assertEqual(self.http.post(url,confirm).status_code,302)
            self.assertEqual(self.http.post(url,confirm).status_code,302)
        self.assertContains(self.http.get(url),'Подтверждена')

    def test_calendar_keeps_moved_plan_identity_and_pre_effective_fact(self):
        from datetime import datetime
        from pool_service.models import ServiceVisitPlan,WaterReading
        row=self.save(schedule=dict(frequency='monthly',first_visit=date(2030,1,15)))
        with patch.object(timezone,'localdate',return_value=date(2030,1,15)):
            service.confirm(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
                revision_id=row.pk,expected_revision=1,effective_from=date(2030,1,15),confirm_effect=True)
        week=date(2030,1,14)
        ServiceVisitPlan.objects.create(pool=self.pool,week_start=week,planned_date=date(2030,1,23),created_by=self.user)
        WaterReading.objects.create(pool=self.pool,added_by=self.user,date=timezone.make_aware(datetime(2030,1,10,12)))
        response=self.http.get(reverse('readings_all'),{'month':'2030-01'})
        self.assertEqual(response.status_code,200)
        items=[item for day in response.context['calendar_days'] for item in day['items'] if item.get('item_type')=='auto']
        moved=[item for item in items if item['date']==date(2030,1,23)]
        self.assertEqual(len(moved),1);self.assertEqual(moved[0]['source_week_start'],'2030-01-14')
        self.assertNotEqual(moved[0]['status'],'done')
        self.assertEqual(sum(item['status']=='done' and item['date']==date(2030,1,10) for item in items),1)

    def test_batch_resolver_and_no_op_update(self):
        from pool_service.services.crm_recurring_effective import attach_terms
        self.prepare_confirmed()
        with self.assertNumQueries(1):attach_terms([self.pool],self.day,self.day+timedelta(days=30))
        with self.assertNumQueries(0):terms_as_of(self.pool,self.day)
        Pool.objects.filter(pk=self.pool.pk).update(service_frequency=self.pool.service_frequency)

    def test_future_confirmation_retains_legacy_notifications_until_effective(self):
        from datetime import datetime
        from pool_service.management.commands import generate_notifications as module
        Pool.objects.filter(pk=self.pool.pk).update(service_frequency='weekly')
        self.org.notify_missed_visits=True;self.org.save()
        self.prepare_confirmed()
        command=module.Command()
        with patch.object(command,'_is_friday_noon',return_value=True),patch.object(module,'notify_org_users') as notify:
            day=date(2029,12,28)
            command._generate_missed_visits(timezone.make_aware(datetime(2029,12,28,12)),day)
            self.assertTrue(notify.called)
            notify.reset_mock()
            command._generate_missed_visits(timezone.make_aware(datetime(2030,1,4,12)),date(2030,1,4))
            self.assertFalse(notify.called)

    def test_confirmed_scope_and_no_retroactive_confirmation(self):
        row=self.save(schedule=dict(frequency=None,first_visit=None))
        with patch.object(timezone,'localdate',return_value=date(2030,1,2)):
            with self.assertRaises(ValidationError):service.confirm(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
                revision_id=row.pk,expected_revision=1,effective_from=date(2030,1,1),confirm_effect=True)
        self.assertFalse(CrmRecurringConfirmation.objects.exists())

    def test_history_removal_and_ambiguous_transition_week_blocked(self):
        from pool_service.services.crm_commercial_commands import CommercialConflict
        self.prepare_confirmed()
        terms=dict(start=date(2030,1,1),end=date(2030,12,31),complete_period_list=True,
            renewal_review_on=date(2030,11,1),periods=[P(date(2030,2,1),Decimal(1),'мес',Decimal(10))])
        row=self.save(action='revise',expected_revision=1,operation_key='removed',terms=terms)
        args=dict(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,confirm_effect=True)
        with patch.object(timezone,'localdate',return_value=date(2030,1,2)):
            with self.assertRaises(CommercialConflict):service.confirm(**args,revision_id=row.pk,expected_revision=2,effective_from=date(2030,2,1))
        row=self.save(action='revise',expected_revision=2,operation_key='two-in-week',schedule=dict(frequency='weekly',first_visit=date(2030,1,3)))
        with patch.object(timezone,'localdate',return_value=date(2030,1,2)):
            with self.assertRaises(CommercialConflict):service.confirm(**args,revision_id=row.pk,expected_revision=3,effective_from=date(2030,1,3))
        self.assertEqual(CrmRecurringConfirmation.objects.count(),1)

    def test_confirmation_rechecks_rights_and_foreign_id(self):
        from pool_service.crm_commercial_models import CrmAccess
        row=self.save()
        args=dict(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,revision_id=row.pk,
            expected_revision=1,effective_from=date(2030,1,1),confirm_effect=True)
        with self.assertRaises(PermissionDenied):service.confirm(**dict(args,actor=self.recipient))
        grant=CrmAccess.objects.get(user=self.user,organization=self.org);grant.edit_own_deals=False;grant.save()
        with self.assertRaises(PermissionDenied):service.confirm(**args)
        response=self.http.get(reverse('crm_recurring',args=[self.deal.pk]),{'org_id':self.org.pk})
        self.assertEqual(response.status_code,403);self.assertEqual(CrmRecurringConfirmation.objects.count(),0)

    def test_renewal_same_lineage_preserves_old_months_and_zero(self):
        first,_=self.prepare_confirmed()
        row=self.renewal(schedule=dict(frequency='monthly',first_visit=date(2031,1,1)))
        service.confirm(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,revision_id=row.pk,
            expected_revision=2,effective_from=date(2031,1,1),confirm_effect=True)
        self.assertEqual(first.draft_id,row.draft_id)
        self.assertIsNone(terms_as_of(self.pool,date(2030,1,1))['price'])
        self.assertEqual(terms_as_of(self.pool,date(2031,1,1))['price'],Decimal('10.50'))
        self.assertEqual(terms_as_of(self.pool,date(2030,12,1))['state'],'outside_period')


    def test_midweek_confirmation_move_and_cross_boundary_plan_single(self):
        import json
        from datetime import datetime
        from pool_service.models import ServiceVisitPlan
        # Jan 2 2030 is Wednesday, Jan 4 Friday.
        Pool.objects.filter(pk=self.pool.pk).update(service_frequency='weekly',created_at=timezone.make_aware(datetime(2029,12,20,12)))
        row=self.save(schedule=dict(frequency='weekly',first_visit=date(2030,1,4)))
        service.confirm(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,revision_id=row.pk,
            expected_revision=1,effective_from=date(2030,1,2),confirm_effect=True)
        plan=ServiceVisitPlan.objects.create(pool=self.pool,week_start=date(2029,12,31),planned_date=date(2030,1,4),created_by=self.user)
        response=self.http.get(reverse('readings_all'),{'month':'2030-01'})
        items=[item for day in response.context['calendar_days'] for item in day['items'] if item.get('item_type')=='auto']
        self.assertEqual(sum(item.get('plan_id')==plan.pk for item in items),1)
        response=self.http.post(reverse('visit_plan_move'),data=json.dumps(dict(pool_ids=[self.pool.pk],source_week_start='2029-12-31',planned_date='2030-01-05')),content_type='application/json')
        self.assertEqual(response.status_code,200,response.content)
        plan.refresh_from_db();self.assertEqual(plan.planned_date,date(2030,1,5))


from django.test import TransactionTestCase,skipUnlessDBFeature
from django.db import close_old_connections,connection
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor


@skipUnlessDBFeature('has_select_for_update')
class ConfirmationRaceTests(ConfirmedFixture,TransactionTestCase):
    def test_confirmation_replay_and_pool_edit_serialize(self):
        row=self.save()
        barrier=Barrier(2)
        def worker(i):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return service.confirm(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
                    revision_id=row.pk,expected_revision=1,effective_from=date(2030,1,1),confirm_effect=True).pk
            finally:close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(worker,range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(CrmRecurringConfirmation.objects.count(),1)
        barrier=Barrier(2)
        def writer(i):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                if i==0:return worker_confirm()
                try:Pool.objects.filter(pk=self.pool.pk).update(service_monthly_price=Decimal(99));return 'write'
                except ValidationError:return 'blocked'
            finally:close_old_connections()
        def worker_confirm():
            return service.confirm(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,
                revision_id=row.pk,expected_revision=1,effective_from=date(2030,1,1),confirm_effect=True).pk
        with ThreadPoolExecutor(max_workers=2) as workers:results=list(workers.map(writer,range(2)))
        self.assertIn('blocked',results);self.assertEqual(CrmRecurringConfirmation.objects.count(),1)


class ConfirmationMigrationTests(ConfirmedFixture,TransactionTestCase):
    def test_confirmation_migration_preserves_drafts_and_operational_values(self):
        from django.db.migrations.executor import MigrationExecutor
        from pool_service.crm_recurring_models import CrmRecurringDraft,CrmRecurringRevision
        self.save();models=(Pool,CrmRecurringDraft,CrmRecurringRevision)
        before={m.__name__:list(m.objects.order_by('pk').values()) for m in models}
        executor=MigrationExecutor(connection);executor.migrate([('pool_service','0166_crm_recurring_drafts')])
        try:
            executor=MigrationExecutor(connection);target=[('pool_service','0167_crm_recurring_confirmation')]
            executor.migrate(target);apps=executor.loader.project_state(target).apps
            for name,rows in before.items():self.assertEqual(list(apps.get_model('pool_service',name).objects.order_by('pk').values()),rows)
            self.assertEqual(apps.get_model('pool_service','CrmRecurringConfirmation').objects.count(),0)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
