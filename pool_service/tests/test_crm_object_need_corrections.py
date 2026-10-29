"""Narrow regressions from independent F1/F2/L1 review of the original freeze."""
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import models
from django.test import TestCase
from django.utils import timezone
from tablib import Dataset

from pool_service.admin import WaterReadingResource
from pool_service.crm_object_need_models import CrmObjectNeed, CrmObjectNeedSourceRevision
from pool_service.models import Client, Pool, ServiceTask, WaterReading
from pool_service.services import crm_object_needs as needs
from pool_service.tests.test_crm_object_needs import ReviewFixture
from pool_service.services import reading_source_notifications as source_push


class MissingOrganizationTests(ReviewFixture, TestCase):
    def test_retained_source_rejects_missing_pool_organization_before_reading_sql(self):
        Pool.objects.filter(pk=self.pool.pk).update(organization=None)
        self.reading.refresh_from_db()
        before = WaterReading.objects.values().get(pk=self.reading.pk)
        self.reading.required_materials = 'Must not detach immutable source history'
        original_save = models.Model.save
        with patch('django.db.models.Model.save', autospec=True, side_effect=original_save) as sql_save:
            with self.assertRaises(ValidationError):
                self.reading.save()
            sql_save.assert_not_called()
        self.assertEqual(WaterReading.objects.values().get(pk=self.reading.pk), before)
        self.assertEqual(self.need.sources.count(), 1)

    def test_unanchored_legacy_supply_graph_also_rejects_missing_org(self):
        reading = WaterReading(pool=self.pool, added_by=self.user, date=timezone.now(), required_materials='Legacy source')
        models.Model.save(reading)
        task = ServiceTask.objects.get(pk=self.task.pk)
        task.pk, task.water_reading, task.crm_item = None, reading, None
        task.save()
        Pool.objects.filter(pk=self.pool.pk).update(organization=None)
        reading.refresh_from_db()
        reading.required_materials = ''
        with self.assertRaises(ValidationError):
            reading.save()
        self.assertEqual(WaterReading.objects.get(pk=reading.pk).required_materials, 'Legacy source')
        self.assertFalse(CrmObjectNeed.objects.filter(reading=reading).exists())

    def test_source_less_orgless_readings_keep_the_existing_baseline(self):
        client = Client.objects.create(name='Unscoped synthetic')
        pool = Pool.objects.create(client=client, organization=None)
        for materials in ('', 'Unscoped text without a supply graph'):
            reading = WaterReading.objects.create(pool=pool, date=timezone.now(), required_materials=materials)
            reading.required_materials = materials + ' edited'
            reading.save()
            reading.refresh_from_db()
            self.assertEqual(reading.required_materials, materials + ' edited')
            self.assertFalse(CrmObjectNeed.objects.filter(reading=reading).exists())
            self.assertFalse(ServiceTask.objects.filter(water_reading=reading).exists())


class ImportPreflightTests(ReviewFixture, TestCase):
    def dataset(self):
        data = Dataset(headers=['id', 'pool', 'date', 'added_by', 'required_materials'])
        data.append(['', self.pool.pk, '2026-10-26 10:00:00', self.user.pk, 'New row takes actor FK'])
        data.append([self.reading.pk, self.pool.pk, '2026-10-26 10:00:00', self.user.pk, 'Existing row needs graph'])
        return data

    def test_failed_upfront_revalidation_stops_every_row_before_save(self):
        original = needs.lock_source_graphs
        count = [0]
        def changed_graph(reading_ids):
            count[0] += 1
            return {} if count[0] == 1 else original(reading_ids)
        resource = WaterReadingResource(source_actor=self.user)
        before = (WaterReading.objects.count(), CrmObjectNeedSourceRevision.objects.count())
        with patch.object(needs, 'lock_source_graphs', side_effect=changed_graph):
            with patch.object(resource, 'before_save_instance', wraps=resource.before_save_instance) as per_row:
                result = resource.import_data(self.dataset(), raise_errors=False)
        self.assertTrue(result.has_errors())
        self.assertEqual(per_row.call_count, 0)
        self.assertEqual(before, (WaterReading.objects.count(), CrmObjectNeedSourceRevision.objects.count()))

    def test_resource_reuse_resets_preflight_even_after_successful_preview(self):
        resource = WaterReadingResource(source_actor=self.user)
        result = resource.import_data(self.dataset(), dry_run=True, raise_errors=True)
        self.assertFalse(result.has_errors())
        self.assertTrue(resource.source_preflight_success)
        with patch.object(resource, 'before_import', side_effect=ValidationError('preflight failure')):
            with patch.object(resource, 'before_save_instance', wraps=resource.before_save_instance) as per_row:
                result = resource.import_data(self.dataset(), raise_errors=False)
        self.assertTrue(result.has_errors())
        self.assertFalse(resource.source_preflight_success)
        self.assertEqual(per_row.call_count, 0)


class UnscopedGenerationGuardTests(ReviewFixture, TestCase):
    def test_sync_never_generates_after_an_unscoped_context_observes_a_new_org(self):
        reading = WaterReading(pool=self.pool, added_by=self.user, date=timezone.now(), required_materials='Unanchored snapshot')
        models.Model.save(reading)
        with patch('pool_service.services.task_generation._create_supply_task_locked') as create:
            with self.assertRaises(needs.NeedConflict):
                needs.synchronize_source(reading, locked_org_id=None)
        create.assert_not_called()
        self.assertFalse(CrmObjectNeed.objects.filter(reading=reading).exists())


class ImportIdentityDecoderTests(ReviewFixture, TestCase):
    def test_loader_numeric_aliases_join_the_same_upfront_reading_graph(self):
        plain = WaterReading.objects.create(pool=self.pool, date=timezone.now())
        for alias in (str(plain.pk), f' {plain.pk} ', f'{plain.pk}.0', f'+{plain.pk}', float(plain.pk)):
            dataset = Dataset(headers=['id', 'pool', 'date', 'comment'])
            dataset.append([alias, self.pool.pk, '2026-10-26 10:00:00', 'Alias update'])
            resource = WaterReadingResource(source_actor=self.user)
            with patch.object(needs, 'lock_source_graphs', wraps=needs.lock_source_graphs) as lock_graph:
                result = resource.import_data(dataset, raise_errors=True)
            self.assertFalse(result.has_errors())
            self.assertIn(plain.pk, lock_graph.call_args_list[0].args[0])
            self.assertIn(plain.pk, resource.prelocked_reading_ids)
            self.assertEqual(WaterReading.objects.get(pk=plain.pk).comment, 'Alias update')

    def test_existing_row_outside_prelocked_set_is_rejected_without_late_lock(self):
        resource = WaterReadingResource(source_actor=self.user)
        resource.source_preflight_success = True
        with patch.object(needs, 'lock_source_graphs') as acquire:
            with self.assertRaises(ValidationError):
                resource.before_save_instance(self.reading, {})
        acquire.assert_not_called()


class SourceCallbackBoundaryTests(ReviewFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.reading.added_by = self.customer
        self.reading.ph = 6.4
        self.reading.save(update_fields=['added_by', 'ph'])

    def test_both_callback_kinds_deliver_with_current_recipient_without_exclusive_user_helper(self):
        with patch('pool_service.operations_mcp_views._locked_staff_user', side_effect=AssertionError('User lock upgrade')):
            for kind, source_id in (('reading', self.reading.pk), ('task', self.task.pk)):
                with self.subTest(kind=kind), patch('pool_service.services.notifications.send_push_to_users') as push:
                    source_push.deliver_source_push(kind, source_id, self.user.pk)
                    push.assert_called_once()

    def test_changed_or_grown_graph_stops_before_recipient_lock_for_both_kinds(self):
        for kind, source_id in (('reading', self.reading.pk), ('task', self.task.pk)):
            with self.subTest(kind=kind), patch.object(needs, 'source_graph_signature', return_value=()):
                with patch.object(source_push, '_recipient') as recipient:
                    self.assertEqual(source_push.deliver_source_push(kind, source_id, self.user.pk), 'graph_changed')
                    recipient.assert_not_called()

    def test_empty_graph_growth_also_stops_before_recipient(self):
        plain = WaterReading.objects.create(pool=self.pool, added_by=self.customer, date=timezone.now(), ph=6.4)
        with patch.object(needs, 'source_graph_signature', return_value=(('new candidate',),)):
            with patch.object(source_push, '_recipient') as recipient:
                self.assertEqual(source_push.deliver_source_push('reading', plain.pk, self.user.pk), 'graph_changed')
                recipient.assert_not_called()

    def test_current_active_role_membership_and_hr_are_required(self):
        for revoke in ('user', 'role', 'membership', 'hr_active', 'hr_dismissed'):
            with self.subTest(revoke=revoke):
                # Isolate each revocation without restoring a deleted membership.
                from django.db import transaction
                with transaction.atomic():
                    if revoke == 'user':
                        type(self.user).objects.filter(pk=self.user.pk).update(is_active=False)
                    elif revoke == 'role':
                        type(self.membership).objects.filter(pk=self.membership.pk).update(role='client')
                    elif revoke == 'membership':
                        self.membership.delete()
                    elif revoke == 'hr_active':
                        type(self.employee).objects.filter(pk=self.employee.pk).update(is_active=False)
                    else:
                        type(self.employee).objects.filter(pk=self.employee.pk).update(employment_status=self.employee.STATUS_DISMISSED)
                    with patch('pool_service.services.notifications.send_push_to_users') as push:
                        source_push.deliver_source_push('reading', self.reading.pk, self.user.pk)
                        push.assert_not_called()
                    transaction.set_rollback(True)

    def test_parent_identity_is_rechecked_after_recipient_wait(self):
        original = source_push._recipient
        def changed_parent(organization, user_id):
            actor = original(organization, user_id)
            Pool.objects.filter(pk=self.pool.pk).update(is_deleted=True)
            return actor
        with patch.object(source_push, '_recipient', side_effect=changed_parent):
            with patch('pool_service.services.notifications.send_push_to_users') as push:
                source_push.deliver_source_push('reading', self.reading.pk, self.user.pk)
                push.assert_not_called()
