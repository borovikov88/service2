"""Real row-lock races selected in MySQL CI; SQLite is deliberately skipped."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from threading import local
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connections, transaction
from django.test import Client as HttpClient, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone
from tablib import Dataset

from pool_service.admin import WaterReadingResource
from pool_service.crm_commercial_models import CrmCommand
from pool_service.crm_object_need_models import CrmObjectNeedReview, CrmObjectNeedSourceRevision
from pool_service.models import CrmItem, Notification, ServiceTask, WaterReading
from pool_service.services import crm_object_needs as needs
from pool_service.services.crm_locking import locked_task_with_crm_graph
from pool_service.services.task_archive import archive_task
from pool_service.services.task_feedback_permissions import lock_feedback_actor
from pool_service.services.task_generation import create_supply_task_from_reading
from pool_service.tests.test_crm_object_needs import ReviewFixture
from pool_service.services.reading_source_notifications import deliver_source_push


@skipUnlessDBFeature('has_select_for_update')
class ObjectNeedConcurrencyTests(ReviewFixture, TransactionTestCase):
    def threaded(self, fn):
        close_old_connections()
        try:
            return fn()
        finally:
            connections['default'].close()

    def together(self, first, second):
        barrier = Barrier(2)
        def run(fn):
            barrier.wait(timeout=10)
            return fn()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.threaded, lambda fn=fn: run(fn)) for fn in (first, second)]
            return [future.result(timeout=25) for future in futures]

    def held_then(self, mutate, contender):
        held, release, started, done = Event(), Event(), Event(), Event()
        def first():
            with transaction.atomic():
                mutate()
                held.set()
                if not release.wait(15): raise RuntimeError('Release timeout')
        def second():
            started.set()
            try:
                return contender()
            finally:
                done.set()
        with ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(self.threaded, first)
            self.assertTrue(held.wait(15))
            b = executor.submit(self.threaded, second)
            try:
                self.assertTrue(started.wait(10))
                self.assertFalse(done.wait(.2), 'Contender did not wait for the held row lock')
            finally:
                release.set()
            a.result(timeout=25)
            return b.result(timeout=25)

    def edit_source(self, text):
        row = WaterReading.objects.get(pk=self.reading.pk)
        row.required_materials = text
        row._source_actor_id = self.user.pk
        row.save()
        return row.pk

    def test_concurrent_terminal_replays_keep_single_identity(self):
        ServiceTask.objects.filter(pk=self.task.pk).update(status='done', is_archived=True)
        before = CrmItem.objects.values().get(pk=self.task.crm_item_id)
        results = self.together(lambda: create_supply_task_from_reading(self.reading).pk,
            lambda: create_supply_task_from_reading(self.reading).pk)
        self.assertEqual(results, [self.task.pk, self.task.pk])
        self.assertEqual(ServiceTask.objects.filter(water_reading=self.reading).count(), 1)
        self.assertEqual(before, CrmItem.objects.values().get(pk=self.task.crm_item_id))
        self.assertEqual(CrmObjectNeedSourceRevision.objects.count(), 1)

    def test_same_key_same_payload_and_different_payload_races(self):
        payload = self.payload()
        results = self.together(lambda: self.execute(payload), lambda: self.execute(payload))
        self.assertEqual(results[0], results[1])
        self.assertEqual(CrmObjectNeedReview.objects.count(), 1)
        different = dict(payload, positions=[dict(payload['positions'][0], reason='Different frozen payload')])
        def conflict():
            try: self.execute(different)
            except needs.NeedConflict: return 'conflict'
            return 'unexpected'
        results = self.together(lambda: self.execute(payload), conflict)
        self.assertEqual(results[1], 'conflict')
        self.assertEqual(CrmCommand.objects.count(), 1)

    def test_concurrent_source_edits_create_two_revisions_without_duplicates(self):
        self.together(lambda: self.edit_source('Source revision A'), lambda: self.edit_source('Source revision B'))
        self.assertEqual(CrmObjectNeedSourceRevision.objects.count(), 3)
        self.assertEqual(ServiceTask.objects.filter(water_reading=self.reading).count(), 1)
        self.reading.refresh_from_db()
        self.assertEqual(self.need.sources.get(sequence=3).materials, self.reading.required_materials)

    def test_source_before_review_rejects_stale_composition_after_wait(self):
        payload = self.payload()
        def review():
            try: self.execute(payload)
            except needs.NeedConflict: return 'conflict'
            return 'unexpected'
        self.assertEqual(self.held_then(lambda: self.edit_source('Changed source while review waits'), review), 'conflict')
        self.assertFalse(CrmObjectNeedReview.objects.exists())

    def test_review_before_source_leaves_historical_review_and_dirty_new_revision(self):
        payload = self.payload()
        self.held_then(lambda: self.execute(payload), lambda: self.edit_source('Source after confirmed review'))
        self.need.refresh_from_db()
        review = CrmObjectNeedReview.objects.get()
        self.assertEqual((review.source.sequence, self.need.source_revision, self.need.review_revision), (1, 2, 1))

    def test_task_archive_before_source_preserves_terminal_task_and_item(self):
        snapshots = {}
        def archive():
            task = locked_task_with_crm_graph(organization=self.org, task_id=self.task.pk)
            archive_task(task, ServiceTask.ARCHIVE_REASON_COMPLETED, self.user)
            snapshots['task'] = ServiceTask.objects.values().get(pk=self.task.pk)
            snapshots['item'] = CrmItem.objects.values().get(pk=self.task.crm_item_id)
        self.held_then(archive, lambda: self.edit_source('Changed after locked archive'))
        self.assertEqual(snapshots['task'], ServiceTask.objects.values().get(pk=self.task.pk))
        self.assertEqual(snapshots['item'], CrmItem.objects.values().get(pk=self.task.crm_item_id))
        self.assertEqual(CrmObjectNeedSourceRevision.objects.count(), 2)

    def test_revoked_grant_and_hr_are_seen_after_lock_wait_on_replay(self):
        payload = self.payload()
        self.execute(payload)
        def replay():
            try: self.execute(payload)
            except PermissionDenied: return 'denied'
            return 'unexpected'
        def revoke():
            grant = type(self.grant).objects.get(pk=self.grant.pk)
            grant.edit_own_deals = False
            grant.save()
        self.assertEqual(self.held_then(revoke, replay), 'denied')
        self.grant.edit_own_deals = True
        self.grant.save()
        self.assertEqual(self.held_then(lambda: type(self.employee).objects.filter(pk=self.employee.pk).update(is_active=False), replay), 'denied')
        self.assertEqual(CrmObjectNeedReview.objects.count(), 1)

    def test_import_locks_entire_graph_before_second_task_feedback_actor(self):
        second = WaterReading.objects.create(pool=self.pool, added_by=self.user, date=timezone.now(), required_materials='Other original source')
        second_task = ServiceTask.objects.get(water_reading=second)
        dataset = Dataset(headers=['id','pool','date','added_by','required_materials'])
        for reading in (self.reading, second):
            dataset.append([reading.pk,self.pool.pk,'2026-10-26 10:00:00',self.user.pk,'Imported ' + str(reading.pk)])
        held, release, started, done = Event(), Event(), Event(), Event()
        original = WaterReadingResource.before_save_instance
        count = [0]
        def pause(resource, instance, row, **kwargs):
            original(resource, instance, row, **kwargs)
            count[0] += 1
            if count[0] == 2:
                held.set()
                if not release.wait(15): raise RuntimeError('Release timeout')
        def import_batch():
            return WaterReadingResource(source_actor=self.user).import_data(dataset, raise_errors=True)
        def feedback():
            started.set()
            with transaction.atomic():
                task = locked_task_with_crm_graph(organization=self.org, task_id=second_task.pk)
                lock_feedback_actor(task, self.user)
                task.description = 'Explicit task feedback after import'
                task.save(update_fields=['description','updated_at'])
            done.set()
        with patch.object(WaterReadingResource, 'before_save_instance', pause), ThreadPoolExecutor(max_workers=2) as executor:
            a = executor.submit(self.threaded, import_batch)
            self.assertTrue(held.wait(15))
            b = executor.submit(self.threaded, feedback)
            try:
                self.assertTrue(started.wait(10))
                self.assertFalse(done.wait(.2))
            finally: release.set()
            self.assertFalse(a.result(timeout=25).has_errors())
            b.result(timeout=25)
        second_task.refresh_from_db()
        self.assertEqual(second_task.description, 'Explicit task feedback after import')
        self.assertEqual(CrmObjectNeedSourceRevision.objects.count(), 4)

    def test_bulk_restore_shared_notification_fk_does_not_deadlock_source_callback(self):
        if connections['default'].vendor != 'mysql':
            self.skipTest('MySQL FK shared-lock interleaving')
        self.membership.role = 'owner'
        self.membership.save(update_fields=['role'])
        readings = [WaterReading.objects.create(pool=self.pool, added_by=self.customer,
            date=timezone.now(), ph=6.4) for _ in range(2)]
        WaterReading.objects.filter(pk__in=[row.pk for row in readings]).update(is_deleted=True)
        Notification.objects.filter(user=self.user, kind='limits').delete()
        self.assertFalse(ServiceTask.objects.filter(water_reading__in=readings).exists())
        bulk_client, second_client = HttpClient(), HttpClient()
        bulk_client.force_login(self.user)
        second_client.force_login(self.user)
        url = reverse('archive_bulk_update') + '?org_id=' + str(self.org.pk)
        selected, release_first, first_note, callback_sending, second_save, bulk_done = [Event() for _ in range(6)]
        state, callback_sql, context = {}, [], local()
        original_save = WaterReading.save

        def restore_save(row, *args, **kwargs):
            if getattr(context, 'bulk', False) and row.pk in {reading.pk for reading in readings}:
                if 'first' not in state:
                    # archive_bulk_update has already materialized both deleted
                    # rows. A concurrent supported restore of B may commit now;
                    # this first bulk still legitimately holds its selected B.
                    state['first'] = row.pk
                    state['second'] = next(reading for reading in readings if reading.pk != row.pk)
                    selected.set()
                    if not release_first.wait(10): raise RuntimeError('First restore release timeout')
                    result = original_save(row, *args, **kwargs)
                    self.assertTrue(Notification.objects.filter(user=self.user, dedupe_key=f'limits:{row.uuid}').exists())
                    first_note.set()  # real INSERT holds User shared FK lock
                    if not callback_sending.wait(10): raise RuntimeError('Callback blocked on shared recipient FK')
                    return result
                second_save.set()  # about to wait on callback's ReadingB lock
            return original_save(row, *args, **kwargs)

        def push(*args, **kwargs):
            if getattr(context, 'callback', False):
                callback_sending.set()
                if not second_save.wait(10): raise RuntimeError('Bulk restore did not reach ReadingB')
                self.assertFalse(bulk_done.wait(.2), 'Restore should wait for callback ReadingB lock')

        def bulk_restore():
            context.bulk = True
            try:
                return bulk_client.post(url, {'archive_action':'restore', 'reading_uuids':[str(row.uuid) for row in readings]})
            finally:
                bulk_done.set()

        def callback():
            context.callback = True
            def capture(execute, sql, params, many, query_context):
                callback_sql.append(sql)
                return execute(sql, params, many, query_context)
            with connections['default'].execute_wrapper(capture):
                deliver_source_push('reading', state['second'].pk, self.user.pk)

        with patch.object(WaterReading, 'save', restore_save), patch('pool_service.services.notifications.send_push_to_users', side_effect=push):
            with ThreadPoolExecutor(max_workers=2) as executor:
                bulk = executor.submit(self.threaded, bulk_restore)
                try:
                    self.assertTrue(selected.wait(10))
                    # Actual second archive endpoint, no task IDs: makes B live
                    # before callback locks it, after the first batch selected it.
                    response = second_client.post(url, {'archive_action':'restore', 'reading_uuids':[str(state['second'].uuid)]})
                    self.assertEqual(response.status_code, 302)
                finally:
                    release_first.set()
                self.assertTrue(first_note.wait(10))
                delivery = executor.submit(self.threaded, callback)
                delivery.result(timeout=20)
                self.assertEqual(bulk.result(timeout=20).status_code, 302)
        user_queries = [sql.upper() for sql in callback_sql if 'FROM `auth_user`' in sql]
        self.assertTrue(any('LOCK IN SHARE MODE' in sql for sql in user_queries), callback_sql)
        self.assertFalse(any('FOR UPDATE' in sql for sql in user_queries), user_queries)
        self.assertFalse(WaterReading.objects.filter(pk__in=[row.pk for row in readings], is_deleted=True).exists())

    def test_both_source_callback_kinds_use_shared_user_without_upgrade(self):
        if connections['default'].vendor != 'mysql':
            self.skipTest('MySQL shared recipient SQL')
        for kind, source_id in (('reading', self.reading.pk), ('task', self.task.pk)):
            statements = []
            def capture(execute, sql, params, many, context):
                statements.append(sql.upper())
                return execute(sql, params, many, context)
            with self.subTest(kind=kind), connections['default'].execute_wrapper(capture):
                deliver_source_push(kind, source_id, self.user.pk)
            user_queries = [sql for sql in statements if 'FROM `AUTH_USER`' in sql]
            self.assertTrue(any('LOCK IN SHARE MODE' in sql for sql in user_queries), statements)
            self.assertFalse(any('FOR UPDATE' in sql for sql in user_queries), user_queries)

    def test_source_callback_observes_current_inactive_user_after_shared_lock_wait(self):
        if connections['default'].vendor != 'mysql':
            self.skipTest('MySQL shared recipient current read')
        self.reading.added_by = self.customer
        self.reading.ph = 6.4
        self.reading.save(update_fields=['added_by', 'ph'])
        with patch('pool_service.services.notifications.send_push_to_users') as push:
            self.held_then(lambda: type(self.user).objects.filter(pk=self.user.pk).update(is_active=False),
                lambda: deliver_source_push('reading', self.reading.pk, self.user.pk))
            push.assert_not_called()
