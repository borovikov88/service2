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
