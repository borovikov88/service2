from datetime import date,datetime,timedelta
from decimal import Decimal
from unittest.mock import patch
from django.test import TestCase,TransactionTestCase,skipUnlessDBFeature
from django.utils import timezone
from django.db import close_old_connections
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pool_service.tests.test_crm_recurring_confirmed import ConfirmedFixture
from pool_service.models import Notification,WaterReading,Profile,ServiceVisitPlan,OrganizationAccess
from pool_service.crm_recurring_models import CrmRecurringReminder
from pool_service.management.commands.generate_notifications import Command
from pool_service.services import crm_recurring_reminders as reminders
from pool_service.services import crm_recurring_drafts as service
from pool_service.services.crm_recurring_terms import ServicePeriodDraft as P


class ReminderFixture(ConfirmedFixture):
    def setUp(self):
        super().setUp()
        self.org.notify_missed_visits=True;self.org.notify_missed_visits_push=True;self.org.save()

    def run_control(self,day):
        now=timezone.make_aware(datetime.combine(day,datetime.min.time()).replace(hour=13))
        with patch.object(timezone,'now',return_value=now):
            Command()._generate_missed_visits(now,day)

    def confirm_row(self,row,day):
        with patch.object(timezone,'localdate',return_value=day):
            return service.confirm(actor=self.user,organization_id=self.org.pk,deal_id=self.deal.pk,revision_id=row.pk,
                expected_revision=row.revision,effective_from=day,confirm_effect=True)

    def reading(self,day,**kw):
        return WaterReading.objects.create(pool=self.pool,added_by=self.user,date=timezone.make_aware(datetime.combine(day,datetime.min.time()).replace(hour=10)),**kw)


class ReminderTests(ReminderFixture,TestCase):
    def test_friday_unknown_price_and_repeated_tick_one_notification_push(self):
        self.prepare_confirmed()
        with patch.object(reminders,'send_push_to_users',return_value=1) as push:
            with self.captureOnCommitCallbacks(execute=True):self.run_control(date(2030,1,3))
            self.assertFalse(Notification.objects.filter(kind='missed_visit').exists())
            with self.captureOnCommitCallbacks(execute=True):self.run_control(date(2030,1,4))
            count=push.call_count;self.assertGreater(count,0)
            first=list(Notification.objects.filter(kind='missed_visit').values_list('pk',flat=True))
            with self.captureOnCommitCallbacks(execute=True):self.run_control(date(2030,1,4))
            self.assertEqual(push.call_count,count)
            self.assertEqual(first,list(Notification.objects.filter(kind='missed_visit').values_list('pk',flat=True)))
            self.assertEqual(CrmRecurringReminder.objects.filter(user=self.user).count(),1)

    def test_visit_completion_only_and_deleted_or_other_org_fact_not_completion(self):
        self.prepare_confirmed();reading=self.reading(date(2030,1,2))
        with patch.object(timezone,'now',return_value=timezone.make_aware(datetime(2030,1,4,13))):
            self.assertEqual(reminders.candidates(self.pool,date(2030,1,4),Command()._period_bounds),[])
            reading.is_deleted=True;reading.save(update_fields=['is_deleted'])
            self.assertEqual(len(reminders.candidates(self.pool,date(2030,1,4),Command()._period_bounds)),1)
        self.assertEqual(Notification.objects.count(),0)

    def test_future_visit_and_manual_move_not_prematurely_missed(self):
        row=self.save(schedule=dict(frequency='monthly',first_visit=date(2030,1,30)))
        self.confirm_row(row,date(2030,1,1))
        self.assertEqual(reminders.candidates(self.pool,date(2030,1,4),Command()._period_bounds),[])
        # First Friday after the month boundary still covers Jan 30, not lost.
        self.assertEqual(len(reminders.candidates(self.pool,date(2030,2,1),Command()._period_bounds)),1)
        ServiceVisitPlan.objects.create(pool=self.pool,week_start=date(2030,1,28),planned_date=date(2030,2,2),created_by=self.user)
        self.assertEqual(reminders.candidates(self.pool,date(2030,2,1),Command()._period_bounds),[])

    def test_moved_older_obligation_and_one_visit_cannot_complete_two(self):
        row=self.save(terms=dict(start=date(2030,1,1),end=date(2030,12,31),complete_period_list=True,renewal_review_on=date(2030,11,1),
            periods=[P(date(2030,5,1),Decimal(1),'мес',None),P(date(2030,6,1),Decimal(1),'мес',None)]),
            schedule=dict(frequency='monthly',first_visit=date(2030,5,1)))
        self.confirm_row(row,date(2030,5,1))
        week=date(2030,4,29)
        ServiceVisitPlan.objects.create(pool=self.pool,week_start=week,planned_date=date(2030,6,5),created_by=self.user)
        items=reminders.candidates(self.pool,date(2030,6,7),Command()._period_bounds)
        self.assertTrue(any(x['key']=='visit:2030-04-29' and x['due']==date(2030,6,5) for x in items))
        self.assertEqual(len(items),2)
        self.reading(date(2030,6,5))
        with patch.object(timezone,'now',return_value=timezone.make_aware(datetime(2030,6,7,13))):
            self.assertEqual(len(reminders.candidates(self.pool,date(2030,6,7),Command()._period_bounds)),1)

    def test_pending_reschedule_records_actual_attempt_without_rewriting_origin(self):
        row,_=self.prepare_confirmed()
        with self.captureOnCommitCallbacks(execute=False):self.run_control(date(2030,1,4))
        ServiceVisitPlan.objects.create(pool=self.pool,week_start=date(2029,12,31),planned_date=date(2030,1,3),created_by=self.user)
        with patch.object(reminders,'send_push_to_users',return_value=1) as push:
            with self.captureOnCommitCallbacks(execute=True):self.run_control(date(2030,1,4))
            self.assertTrue(all('03.01.2030' in call.kwargs['message'] for call in push.call_args_list))
        receipt=CrmRecurringReminder.objects.get(user=self.user)
        self.assertEqual(receipt.due_date,date(2030,1,1))
        self.assertEqual(receipt.attempted_due_date,date(2030,1,3))
        self.assertEqual(receipt.attempted_revision_id,row.pk)

    def test_known_unattempted_callback_resumes_and_zero_is_not_delivery_proof(self):
        self.prepare_confirmed()
        with self.captureOnCommitCallbacks(execute=False):self.run_control(date(2030,1,4))
        self.assertTrue(CrmRecurringReminder.objects.filter(push_attempted_at__isnull=True,push_result='pending').exists())
        with patch.object(reminders,'send_push_to_users',return_value=0) as push:
            with self.captureOnCommitCallbacks(execute=True):self.run_control(date(2030,1,4))
            count=push.call_count;self.assertGreater(count,0)
            with self.captureOnCommitCallbacks(execute=True):self.run_control(date(2030,1,4))
            self.assertEqual(push.call_count,count)
        self.assertTrue(CrmRecurringReminder.objects.filter(push_result='no_delivery_confirmed').exists())

    def test_expired_term_recent_due_retained_but_no_historical_replay(self):
        row=self.save(terms=dict(start=date(2030,1,1),end=date(2030,1,31),complete_period_list=True,renewal_review_on=date(2030,1,20),
            periods=[P(date(2030,1,1),Decimal(1),'мес',None)]),schedule=dict(frequency='monthly',first_visit=date(2030,1,30)))
        self.confirm_row(row,date(2030,1,1))
        self.assertEqual(len(reminders.candidates(self.pool,date(2030,2,1),Command()._period_bounds)),1)
        self.assertEqual(reminders.candidates(self.pool,date(2030,3,1),Command()._period_bounds),[])

    def test_future_terms_do_not_disable_legacy_and_unknown_schedule_is_explicit(self):
        row=self.save();self.confirm_row(row,date(2030,1,1))
        self.assertIsNone(reminders.candidates(self.pool,date(2029,12,28),Command()._period_bounds))
        items=reminders.candidates(self.pool,date(2030,1,4),Command()._period_bounds)
        self.assertEqual(items[0]['key'],'schedule:2030-01')
        self.assertIn('пропуск посещения не установлен',items[0]['text'])

    def test_push_only_prefs_retry_and_unknown_transport_outcome(self):
        self.prepare_confirmed();self.org.notify_missed_visits=False;self.org.save()
        with patch.object(reminders,'send_push_to_users',side_effect=RuntimeError('synthetic transport failure')) as push:
            with self.captureOnCommitCallbacks(execute=True):self.run_control(date(2030,1,4))
            first=push.call_count;self.assertGreater(first,0)
            with self.captureOnCommitCallbacks(execute=True):self.run_control(date(2030,1,4))
            self.assertEqual(push.call_count,first)
        self.assertFalse(Notification.objects.filter(kind='missed_visit').exists())
        self.assertTrue(CrmRecurringReminder.objects.filter(push_result='uncertain').exists())

    def test_push_rechecks_completion_and_role_before_transport(self):
        self.prepare_confirmed()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:self.run_control(date(2030,1,4))
        self.reading(date(2030,1,2))
        with patch.object(timezone,'now',return_value=timezone.make_aware(datetime(2030,1,4,13))),patch.object(reminders,'send_push_to_users') as push:
            for callback in callbacks:callback()
            self.assertFalse(push.called)
        self.assertFalse(CrmRecurringReminder.objects.filter(push_result='attempting').exists())

    def test_legacy_notification_and_revision_do_not_duplicate_same_obligation(self):
        row,_=self.prepare_confirmed()
        Notification.objects.create(user=self.user,organization=self.org,pool=self.pool,kind='missed_visit',title='Synthetic existing',
            dedupe_key=f'missed_visit:{self.pool.pk}:2030-W01')
        self.run_control(date(2030,1,4))
        self.assertEqual(Notification.objects.filter(user=self.user,kind='missed_visit').count(),1)
        self.assertEqual(CrmRecurringReminder.objects.get(user=self.user).push_result,'covered_legacy')
        edited=self.save(action='revise',expected_revision=1,operation_key='price-only',schedule=dict(frequency='weekly',first_visit=date(2030,1,1)))
        self.confirm_row(edited,date(2030,1,2))
        self.run_control(date(2030,1,4))
        self.assertEqual(Notification.objects.filter(user=self.user,kind='missed_visit').count(),1)


@skipUnlessDBFeature('has_select_for_update')
class ReminderRaceTests(ReminderFixture,TransactionTestCase):
    def test_two_scheduled_runs_one_receipt_per_user(self):
        self.prepare_confirmed();barrier=Barrier(2)
        def worker(_):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return reminders.generate_for_pool(self.pool.pk,date(2030,1,4),Command()._period_bounds)
            finally:close_old_connections()
        with patch.object(reminders,'send_push_to_users',return_value=1) as push:
            with ThreadPoolExecutor(max_workers=2) as workers:list(workers.map(worker,range(2)))
            self.assertEqual(CrmRecurringReminder.objects.filter(user=self.user).count(),1)
            self.assertEqual(Notification.objects.filter(user=self.user,kind='missed_visit').count(),1)
            self.assertEqual(push.call_count,CrmRecurringReminder.objects.count())

    def _wait_for_lock_recheck(self, change):
        from django.db import transaction,connection
        from threading import Event
        from pool_service.models import Organization
        self.prepare_confirmed();entered=Event();original=reminders._pool_org_id
        def seed(pk):
            result=original(pk);entered.set();return result
        def worker():
            close_old_connections()
            try:
                with connection.cursor() as cursor:cursor.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                return reminders.generate_for_pool(self.pool.pk,date(2030,1,4),Command()._period_bounds)
            finally:close_old_connections()
        with patch.object(reminders,'_pool_org_id',side_effect=seed),patch.object(reminders,'send_push_to_users') as push,patch.object(timezone,'now',return_value=timezone.make_aware(datetime(2030,1,4,13))):
            with ThreadPoolExecutor(max_workers=1) as workers:
                with transaction.atomic():
                    Organization.objects.select_for_update().get(pk=self.org.pk)
                    future=workers.submit(worker)
                    self.assertTrue(entered.wait(10))
                    change()
                self.assertTrue(future.result(timeout=15))
            self.assertFalse(push.called)
        self.assertEqual(CrmRecurringReminder.objects.count(),0)

    def test_repeatable_read_rechecks_completed_visit_after_waiting(self):
        self._wait_for_lock_recheck(lambda:self.reading(date(2030,1,2)))

    def test_repeatable_read_rechecks_revoked_roles_after_waiting(self):
        self._wait_for_lock_recheck(lambda:OrganizationAccess.objects.filter(organization=self.org).update(role='accountant'))


class ReminderMigrationTests(ReminderFixture,TransactionTestCase):
    def test_receipt_migration_preserves_contracts_and_existing_notifications(self):
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor
        from pool_service.models import Pool
        from pool_service.crm_recurring_models import CrmRecurringConfirmation,CrmRecurringRevision
        self.prepare_confirmed()
        Notification.objects.create(user=self.user,organization=self.org,pool=self.pool,kind='missed_visit',title='Synthetic retained')
        models=(Pool,CrmRecurringConfirmation,CrmRecurringRevision,Notification)
        before={m.__name__:list(m.objects.order_by('pk').values()) for m in models}
        executor=MigrationExecutor(connection);executor.migrate([('pool_service','0167_crm_recurring_confirmation')])
        try:
            executor=MigrationExecutor(connection);target=[('pool_service','0168_crm_recurring_reminder_receipts')]
            executor.migrate(target);apps=executor.loader.project_state(target).apps
            for name,rows in before.items():self.assertEqual(list(apps.get_model('pool_service',name).objects.order_by('pk').values()),rows)
            self.assertEqual(apps.get_model('pool_service','CrmRecurringReminder').objects.count(),0)
        finally:
            executor=MigrationExecutor(connection);executor.migrate(executor.loader.graph.leaf_nodes())
