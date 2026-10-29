"""Real held-lock tests selected by MySQL CI; SQLite cannot prove these races."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections, transaction
from django.test import TransactionTestCase, skipUnlessDBFeature

from pool_service.crm_commercial_models import CrmDeal
from pool_service.crm_object_need_models import CrmObjectNeedDealLine
from pool_service.models import ServiceTask
from pool_service.services.crm_object_needs import NeedConflict
from pool_service.services.crm_locking import locked_task_with_crm_graph
from pool_service.services.task_feedback_permissions import lock_feedback_actor
from pool_service.tests.test_crm_need_deals import NeedDealFixture


@skipUnlessDBFeature('has_select_for_update')
class NeedDealConcurrencyTests(NeedDealFixture, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.reconcile()

    def threaded(self, action):
        close_old_connections()
        try: return action()
        finally: connections['default'].close()

    def held_then(self, mutate, contender):
        held, release, started, done = Event(), Event(), Event(), Event()
        def holder():
            with transaction.atomic():
                mutate(); held.set()
                if not release.wait(15): raise RuntimeError('Held-lock release timeout')
        def waiter():
            started.set()
            try: return contender()
            finally: done.set()
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(self.threaded, holder)
            self.assertTrue(held.wait(15))
            second = executor.submit(self.threaded, waiter)
            try:
                self.assertTrue(started.wait(10))
                self.assertFalse(done.wait(.2), 'Conversion must wait for actual held row locks')
            finally: release.set()
            first.result(timeout=25)
            return second.result(timeout=25)

    def outcome(self, payload):
        try: return self.convert(payload)
        except (PermissionDenied, ValidationError): return 'denied'

    def test_same_key_competing_requests_return_same_ids(self):
        payload = self.convert_payload()
        gate = Barrier(2)
        def attempt():
            gate.wait(timeout=10)
            return self.convert(payload)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.threaded, attempt) for _ in range(2)]
            results = [future.result(timeout=25) for future in futures]
        self.assertEqual(results[0],results[1])
        self.assertEqual(CrmDeal.objects.count(),1)
        self.assertEqual(CrmObjectNeedDealLine.objects.count(),2)

    def test_new_key_after_held_conversion_cannot_consume_same_group(self):
        payload = self.convert_payload()
        other = dict(payload,operation_key='competing-new-key')
        self.assertEqual(self.held_then(lambda:self.convert(payload),lambda:self.outcome(other)), 'denied')
        self.assertEqual(CrmDeal.objects.count(),1)

    def test_changed_source_is_seen_after_wait_and_existing_conversion_survives(self):
        payload = self.convert_payload()
        def source_edit():
            row = type(self.reading).objects.get(pk=self.reading.pk)
            row.required_materials = 'Changed source while conversion waits'
            row.save()
        self.assertEqual(self.held_then(source_edit,lambda:self.outcome(payload)), 'denied')
        self.assertFalse(CrmDeal.objects.exists())

    def test_replay_locks_recorded_followup_before_actor_and_keeps_single_result(self):
        payload = self.convert_payload()
        result = self.convert(payload)
        def feedback():
            task = locked_task_with_crm_graph(organization=self.org,task_id=result['task_id'])
            lock_feedback_actor(task,self.user)
            task.title = 'Updated by existing task editor'
            task.save(update_fields=['title','updated_at'])
        self.assertEqual(self.held_then(feedback,lambda:self.convert(payload)),result)
        self.assertEqual(CrmDeal.objects.count(),1)

    def test_primary_and_participant_changes_before_conversion_deny_current_source(self):
        payload = self.convert_payload()
        def revoke():
            task = locked_task_with_crm_graph(organization=self.org,task_id=self.task.pk)
            lock_feedback_actor(task,self.user)
            task.primary_responsible = None
            task.responsibles.clear()
            task.save(update_fields=['primary_responsible','updated_at'])
        self.assertEqual(self.held_then(revoke,lambda:self.outcome(payload)), 'denied')
        self.assertFalse(CrmDeal.objects.exists())

    def test_grant_and_hr_revocation_are_seen_on_replay_after_wait(self):
        payload = self.convert_payload()
        self.convert(payload)
        def revoke():
            grant = type(self.grant).objects.get(pk=self.grant.pk)
            grant.create_own_deals = False
            grant.save()
        self.assertEqual(self.held_then(revoke,lambda:self.outcome(payload)), 'denied')
        self.grant.create_own_deals = True; self.grant.save()
        self.assertEqual(self.held_then(lambda:type(self.employee).objects.filter(pk=self.employee.pk).update(is_active=False),
            lambda:self.outcome(payload)), 'denied')
        self.assertEqual(CrmDeal.objects.count(),1)

    def test_new_review_before_conversion_refuses_stale_review(self):
        payload = self.convert_payload()
        def review():
            self.execute(operation_key='concurrent-review',positions=self.payload()['positions'])
        self.assertEqual(self.held_then(review,lambda:self.outcome(payload)), 'denied')
        self.assertFalse(CrmDeal.objects.exists())

    def test_committed_replay_after_held_new_review_keeps_original_result(self):
        payload = self.convert_payload()
        result = self.convert(payload)
        def review():
            self.execute(operation_key='held-review-after-commit', positions=self.payload()['positions'])
        self.assertEqual(self.held_then(review, lambda: self.convert(payload)), result)
        self.assertEqual(CrmDeal.objects.count(), 1)
        self.assertEqual(CrmObjectNeedDealLine.objects.count(), 2)

    def test_converted_history_kept_when_source_changes_after_held_conversion(self):
        payload = self.convert_payload()
        def edit():
            row = type(self.reading).objects.get(pk=self.reading.pk)
            row.required_materials = 'Source after retained conversion'
            row.save()
        self.held_then(lambda:self.convert(payload),edit)
        self.assertEqual(CrmDeal.objects.count(),1)
        self.assertEqual(CrmObjectNeedDealLine.objects.count(),2)
        self.assertEqual(self.need.sources.count(),2)
