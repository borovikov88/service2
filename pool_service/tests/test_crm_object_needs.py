"""Synthetic regressions for stable object needs; no external providers."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from pool_service.models import Client, CrmItem, Organization, OrganizationAccess, Pool, ServiceTask, WaterReading
from django.contrib.auth.models import User
from pool_service.services.task_generation import create_supply_task_from_reading


class NeedFixture:
    def setUp(self):
        self.org = Organization.objects.create(name='Need synthetic', crm_v2_read=True, crm_v2_write=True, trial_started_at=timezone.now())
        self.user = User.objects.create_user('need-manager')
        self.membership = OrganizationAccess.objects.create(user=self.user, organization=self.org, role='manager')
        self.customer = User.objects.create_user('need-client')
        self.client_record = Client.objects.create(user=self.customer, organization=self.org, name='Synthetic customer')
        self.pool = Pool.objects.create(organization=self.org, client=self.client_record, address='Synthetic object')
        self.reading = WaterReading.objects.create(pool=self.pool, added_by=self.user, date=timezone.now(), required_materials='Chlorine and pump')
        self.task = ServiceTask.objects.get(water_reading=self.reading)


class StableSupplyTests(NeedFixture, TestCase):
    def test_terminal_and_archived_source_replay_keeps_identity_and_frozen_state(self):
        for status in ('waiting', 'done', 'cancelled'):
            self.task.status = status
            self.task.is_archived = status != 'waiting'
            self.task.save()
            before = ServiceTask.objects.values().get(pk=self.task.pk)
            item_before = CrmItem.objects.values().get(pk=self.task.crm_item_id)
            result = create_supply_task_from_reading(self.reading)
            self.assertEqual(result.pk, self.task.pk)
            self.assertEqual(ServiceTask.objects.filter(water_reading=self.reading).count(), 1)
            self.assertEqual(ServiceTask.objects.values().get(pk=self.task.pk), before)
            self.assertEqual(CrmItem.objects.values().get(pk=self.task.crm_item_id), item_before)

    def test_source_edit_refreshes_only_generated_open_composition(self):
        self.reading.required_materials = 'Replacement hose'
        self.reading.save()
        self.task.refresh_from_db()
        self.assertIn('Replacement hose', self.task.description)
        self.assertNotIn('Chlorine and pump', self.task.description)
        self.assertEqual(self.task.crm_item.equipment_replacement, 'Replacement hose')

    def test_ambiguous_legacy_source_rejects_without_picking_first(self):
        # Duplicate insertion simulates trusted historical SQL, not a new writer.
        duplicate = ServiceTask.objects.get(pk=self.task.pk)
        duplicate.pk = None
        duplicate.crm_item = None
        duplicate.save()
        self.reading.required_materials = 'Ambiguous replacement'
        with self.assertRaises(ValidationError):
            self.reading.save()
        self.reading.refresh_from_db()
        self.assertEqual(self.reading.required_materials, 'Chlorine and pump')


from unittest.mock import patch
import json
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import reverse
from pool_service.crm_commercial_models import CrmAccess, CrmCommand, CrmDeal
from pool_service.crm_object_need_models import CrmObjectNeed, CrmObjectNeedReview, CrmObjectNeedSourceRevision
from pool_service.models import Employee
from pool_service.services.crm_object_needs import NeedConflict, materials_hash, review_need
from pool_service.services.task_feedback import version_for


class ReviewFixture(NeedFixture):
    def setUp(self):
        super().setUp()
        self.grant = CrmAccess.objects.create(organization=self.org, user=self.user, edit_own_deals=True, view_team_deals=True)
        self.employee = Employee.objects.create(organization=self.org, user=self.user, display_name='Synthetic manager')
        self.need = CrmObjectNeed.objects.get(reading=self.reading)

    def payload(self, **kwargs):
        self.need.refresh_from_db()
        self.task.refresh_from_db()
        data = dict(task_id=self.task.pk, source_revision=self.need.source_revision,
            source_hash=materials_hash(self.reading.required_materials.strip()), review_revision=self.need.review_revision,
            task_version=version_for(self.task), operation_key='need-review-1', positions=[
                {'line_token':'chemical_1', 'description':'Chlorine', 'mode':'chemistry_monthly', 'reason':'Manager checked monthly conditions'},
                {'line_token':'pump_1', 'description':'Pump', 'mode':'approval_required', 'reason':'Scope and price need approval'},
                {'line_token':'hose_1', 'description':'Hose', 'mode':'included', 'reason':'Manager checked service terms'},
                {'line_token':'old_1', 'description':'Old part', 'mode':'already_supplied', 'reason':'Manager checked issue record'},
            ])
        data.update(kwargs)
        return data

    def execute(self, data=None, **kwargs):
        return review_need(actor=self.user, organization=self.org, **(data or self.payload(**kwargs)))


class NeedReviewTests(ReviewFixture, TestCase):
    def test_mixed_review_replay_payload_freeze_and_no_task_or_deal_mutation(self):
        data = self.payload()
        before_task = ServiceTask.objects.values().get(pk=self.task.pk)
        before_item = CrmItem.objects.values().get(pk=self.task.crm_item_id)
        result = self.execute(data)
        self.assertEqual(self.execute(data), result)
        self.assertEqual(CrmCommand.objects.count(), 1)
        self.assertEqual(CrmObjectNeedReview.objects.count(), 1)
        self.assertEqual(ServiceTask.objects.values().get(pk=self.task.pk), before_task)
        self.assertEqual(CrmItem.objects.values().get(pk=self.task.crm_item_id), before_item)
        self.assertFalse(CrmDeal.objects.exists())
        data['positions'][0]['reason'] = 'Changed payload'
        with self.assertRaises(NeedConflict): self.execute(data)

    def test_source_change_and_clear_are_immutable_dirty_and_old_review_is_history_only(self):
        data = self.payload()
        result = self.execute(data)
        self.reading.required_materials = 'New source composition'
        self.reading.save()
        self.need.refresh_from_db()
        self.assertEqual(self.need.source_revision, 2)
        self.assertEqual(self.need.review_revision, 1)
        self.assertEqual(CrmObjectNeedReview.objects.get(pk=result['review_id']).source.sequence, 1)
        with self.assertRaises(NeedConflict): self.execute(data)
        self.client.force_login(self.user)
        response = self.client.get(reverse('crm_object_need_detail', args=[self.task.pk]), {'org_id': self.org.pk})
        self.assertEqual(response.status_code, 200)
        row = response.json()['need']
        self.assertEqual((row['state'], row['positions']), ('changed', []))
        self.reading.required_materials = ''
        self.reading.save()
        self.need.refresh_from_db()
        self.assertEqual(self.need.source_revision, 3)
        self.assertEqual(self.need.sources.get(sequence=3).materials, '')
        self.assertEqual(ServiceTask.objects.filter(water_reading=self.reading).count(), 1)
        self.reading.save()
        self.assertEqual(self.need.sources.count(), 3)
        for model in (CrmObjectNeedSourceRevision, CrmObjectNeedReview):
            row = model.objects.first()
            with self.assertRaises(ValidationError): row.save()
            with self.assertRaises(ValidationError): model.objects.all().update(sequence=99)
            with self.assertRaises(ValidationError): row.delete()

    def test_frozen_source_edit_preserves_entire_task_item_and_records_revision(self):
        self.execute()
        for status in ('waiting', 'done', 'cancelled'):
            self.task.refresh_from_db()
            self.task.status = status
            self.task.is_archived = status != 'waiting'
            self.task.save()
            before_task = ServiceTask.objects.values().get(pk=self.task.pk)
            before_item = CrmItem.objects.values().get(pk=self.task.crm_item_id)
            self.reading.required_materials = 'Changed after ' + status
            self.reading.save()
            self.assertEqual(ServiceTask.objects.values().get(pk=self.task.pk), before_task)
            self.assertEqual(CrmItem.objects.values().get(pk=self.task.crm_item_id), before_item)
            with self.assertRaises(PermissionDenied): self.execute(operation_key='status-' + status)
        self.assertEqual(self.need.sources.count(), 4)

    def test_manual_fields_survive_source_edits_and_soft_delete_does_not_enter_source_locks(self):
        self.task.description = 'Manual task description'
        self.task.save()
        item = self.task.crm_item
        item.description = 'Manual item description'
        item.equipment_replacement = 'Manual item composition'
        item.save()
        self.reading.required_materials = 'Updated source'
        self.reading.save()
        self.task.refresh_from_db()
        item.refresh_from_db()
        self.assertEqual(self.task.description, 'Manual task description')
        self.assertEqual(item.description, 'Manual item description')
        self.assertEqual(item.equipment_replacement, 'Manual item composition')
        self.reading.is_deleted = True
        with patch('pool_service.services.crm_object_needs.source_write_context', side_effect=AssertionError('Must bypass')):
            self.reading.save(update_fields=['is_deleted'])
        self.assertEqual(self.need.sources.count(), 2)

    def test_source_error_and_review_error_roll_back_all_rows(self):
        self.reading.required_materials = 'Will roll back'
        with patch.object(CrmObjectNeedSourceRevision, 'save', side_effect=ValidationError('forced source failure')):
            with self.assertRaises(ValidationError): self.reading.save()
        self.reading.refresh_from_db()
        self.assertEqual(self.reading.required_materials, 'Chlorine and pump')
        self.assertEqual(self.need.sources.count(), 1)
        with patch.object(CrmObjectNeedReview, 'save', side_effect=ValidationError('forced review failure')):
            with self.assertRaises(ValidationError): self.execute()
        self.assertEqual(CrmCommand.objects.count(), 0)
        self.assertEqual(CrmObjectNeedReview.objects.count(), 0)
        self.need.refresh_from_db()
        self.assertEqual(self.need.review_revision, 0)

    def test_current_flags_account_hr_grant_membership_and_ownership_rechecked_on_replay(self):
        data = self.payload()
        self.execute(data)
        for model, pk, change, restore in (
            (User, self.user.pk, {'is_active':False}, {'is_active':True}),
            (Employee, self.employee.pk, {'is_active':False}, {'is_active':True}),
            (Organization, self.org.pk, {'crm_v2_read':False}, {'crm_v2_read':True}),
            (Organization, self.org.pk, {'crm_v2_write':False}, {'crm_v2_write':True}),
            (ServiceTask, self.task.pk, {'primary_responsible':None}, {'primary_responsible':self.user}),
        ):
            model.objects.filter(pk=pk).update(**change)
            with self.assertRaises((PermissionDenied, NeedConflict)): self.execute(data)
            model.objects.filter(pk=pk).update(**restore)
        self.grant.edit_own_deals = False
        self.grant.save()
        with self.assertRaises(PermissionDenied): self.execute(data)
        self.grant.edit_own_deals = True
        self.grant.save()
        self.membership.delete()
        with self.assertRaises(PermissionDenied): self.execute(data)
        self.assertEqual(CrmObjectNeedReview.objects.count(), 1)

    def test_primary_alone_and_owner_admin_cannot_edit_other_own_or_private_task(self):
        self.task.created_by = self.customer
        self.task.responsibles.clear()
        self.task.save()
        with self.assertRaises(PermissionDenied): self.execute()
        for role in ('owner', 'admin'):
            self.membership.role = role
            self.membership.save()
            self.task.primary_responsible = self.customer
            self.task.save()
            with self.assertRaises(PermissionDenied): self.execute()

    def test_positions_and_versions_are_bounded_and_strict(self):
        valid = self.payload()
        bad_positions = ([], valid['positions'] * 20, [{'line_token':'one', 'description':'x', 'mode':'included','reason':''}],
            [{'line_token':'one', 'description':'x' * 1001, 'mode':'included','reason':'checked'}],
            [{'line_token':'one', 'description':'x', 'mode':'delivered','reason':'checked'}],
            [{'line_token':'one', 'description':'x', 'mode':'included','reason':'checked','price':123}])
        for positions in bad_positions:
            with self.assertRaises(ValidationError): self.execute(positions=positions)
        for changes in ({'source_revision':True}, {'review_revision':1}, {'task_version':'0'*64},
                {'operation_key':'BAD'}, {'source_hash':'0'*64}):
            with self.assertRaises(ValidationError): self.execute(**changes)
        self.assertFalse(CrmCommand.objects.exists())
        self.assertFalse(CrmObjectNeedReview.objects.exists())

    def test_private_or_reparented_source_graph_denied_on_replay(self):
        data = self.payload()
        self.execute(data)
        for payload in ({'source_call_id':99999}, {'source':'call_analysis'}):
            ServiceTask.objects.filter(pk=self.task.pk).update(payload_json=payload)
            with self.assertRaises(PermissionDenied): self.execute(data)
        ServiceTask.objects.filter(pk=self.task.pk).update(payload_json={})
        other = Organization.objects.create(name='Foreign synthetic')
        Client.objects.filter(pk=self.client_record.pk).update(organization=other)
        with self.assertRaises((PermissionDenied, ValidationError)): self.execute(data)
        self.assertEqual(CrmObjectNeedReview.objects.count(), 1)


class NeedQueueTests(ReviewFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_scoped_queue_search_filters_and_50_pagination(self):
        for index in range(53):
            WaterReading.objects.create(pool=self.pool, added_by=self.user, date=timezone.now(), required_materials='Visible source ' + str(index))
        hidden = User.objects.create_user('hidden-synthetic')
        reading = WaterReading.objects.create(pool=self.pool, added_by=hidden, date=timezone.now(), required_materials='Hidden needle')
        task = ServiceTask.objects.get(water_reading=reading)
        task.created_by = hidden
        task.primary_responsible = hidden
        task.responsibles.set([hidden])
        task.save()
        url = reverse('crm_object_need_data')
        result = self.client.get(url, {'org_id':self.org.pk}).json()
        self.assertEqual((result['total'], len(result['rows']), result['pages']), (54, 50, 2))
        result = self.client.get(url, {'org_id':self.org.pk, 'q':'Hidden needle'}).json()
        self.assertEqual((result['total'], result['rows']), (0, []))
        result = self.client.get(url, {'org_id':self.org.pk, 'q':'Visible source 5', 'scope':'mine'}).json()
        self.assertEqual(result['total'], 4)
        self.assertEqual(self.client.get(reverse('crm_object_need_detail', args=[task.pk]), {'org_id':self.org.pk}).status_code, 404)
        self.execute()
        result = self.client.get(url, {'org_id':self.org.pk, 'mode':'included', 'state':'reviewed'}).json()
        self.assertEqual(result['total'], 1)

    def test_available_participant_can_read_but_cannot_review_others_task(self):
        other = User.objects.create_user('other-primary')
        OrganizationAccess.objects.create(user=other, organization=self.org, role='manager')
        self.task.primary_responsible = other
        self.task.save()
        response = self.client.get(reverse('crm_object_need_detail', args=[self.task.pk]), {'org_id':self.org.pk})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['need']['can_review'])
        response = self.client.post(reverse('crm_object_need_review') + '?org_id=' + str(self.org.pk), data=json.dumps(self.payload()), content_type='application/json')
        self.assertEqual(response.status_code, 403)

    def test_reading_scope_and_http_validation_with_no_get_side_effects(self):
        before = (CrmObjectNeed.objects.count(), CrmObjectNeedSourceRevision.objects.count(), CrmCommand.objects.count())
        self.assertEqual(self.client.get(reverse('crm_object_needs'), {'org_id':self.org.pk}).status_code, 200)
        self.assertEqual(self.client.get(reverse('crm_object_need_data'), {'org_id':self.org.pk}).status_code, 200)
        self.assertEqual(before, (CrmObjectNeed.objects.count(), CrmObjectNeedSourceRevision.objects.count(), CrmCommand.objects.count()))
        payload = self.payload()
        payload['new_api_field'] = 'forbidden'
        response = self.client.post(reverse('crm_object_need_review') + '?org_id=' + str(self.org.pk), data=json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get(reverse('crm_object_need_review'), {'org_id':self.org.pk}).status_code, 405)
        self.reading.is_deleted = True
        self.reading.save(update_fields=['is_deleted'])
        response = self.client.get(reverse('crm_object_need_data'), {'org_id':self.org.pk})
        self.assertEqual(response.json()['total'], 0)


from tablib import Dataset
from pool_service.admin import WaterReadingResource


class ExistingReadingImportTests(NeedFixture, TestCase):
    def dataset(self):
        other_org = Organization.objects.create(name='Other import synthetic')
        other_client = Client.objects.create(organization=other_org, name='Other synthetic')
        other_pool = Pool.objects.create(organization=other_org, client=other_client)
        dataset = Dataset(headers=['id', 'pool', 'date', 'added_by', 'required_materials'])
        dataset.append(['', other_pool.pk, '2026-10-26 10:00:00', self.user.pk, 'Second org first'])
        dataset.append([self.reading.pk, self.pool.pk, '2026-10-26 10:00:00', self.user.pk, 'Original source changed'])
        return dataset

    def test_ordered_multi_org_batch_and_actual_actor_provenance(self):
        resource = WaterReadingResource(source_actor=self.customer)
        result = resource.import_data(self.dataset(), raise_errors=True)
        self.assertFalse(result.has_errors())
        self.assertFalse(result.has_validation_errors())
        self.assertEqual(CrmObjectNeed.objects.count(), 2)
        source = CrmObjectNeedSourceRevision.objects.filter(need__reading=self.reading).last()
        self.assertEqual(source.actor_id, self.customer.pk)
        self.reading.refresh_from_db()
        self.assertEqual(self.reading.added_by_id, self.user.pk)
        self.assertEqual(resource.source_org_ids, set(Organization.objects.values_list('pk', flat=True)))

    def test_invalid_late_row_rolls_back_entire_batch_and_dry_run_has_no_push(self):
        dataset = self.dataset()
        dataset.append(['', 99999999, 'invalid date', self.user.pk, 'Invalid late row'])
        before = (WaterReading.objects.count(), CrmObjectNeed.objects.count(), CrmObjectNeedSourceRevision.objects.count())
        resource = WaterReadingResource(source_actor=self.customer)
        with patch('pool_service.services.notifications.send_push_to_users') as push, self.captureOnCommitCallbacks(execute=True):
            result = resource.import_data(dataset)
        self.assertTrue(result.has_errors() or result.has_validation_errors())
        self.assertEqual(before, (WaterReading.objects.count(), CrmObjectNeed.objects.count(), CrmObjectNeedSourceRevision.objects.count()))
        push.assert_not_called()
        with patch('pool_service.services.notifications.send_push_to_users') as push, self.captureOnCommitCallbacks(execute=True):
            del dataset[-1]
            result = resource.import_data(dataset, dry_run=True, raise_errors=True)
        self.assertFalse(result.has_errors())
        self.assertEqual(before, (WaterReading.objects.count(), CrmObjectNeed.objects.count(), CrmObjectNeedSourceRevision.objects.count()))
        push.assert_not_called()


class NeedAdditionalBoundariesTests(ReviewFixture, TestCase):
    def test_source_participant_without_individual_read_grant_cannot_open_crm(self):
        self.membership.role = 'service'
        self.membership.save()
        self.grant.view_team_deals = False
        self.grant.save()
        self.client.force_login(self.user)
        for role in ('service', 'installer', 'manager'):
            self.membership.role = role
            self.membership.save()
            response = self.client.get(reverse('crm_object_need_detail', args=[self.task.pk]), {'org_id':self.org.pk})
            self.assertEqual(response.status_code, 403)
            with self.assertRaises(PermissionDenied): self.execute()

    def test_legacy_get_does_not_create_anchor_first_authorized_review_is_replayable(self):
        from django.db import models
        reading = WaterReading(pool=self.pool, added_by=self.user, date=timezone.now(), required_materials='Legacy source')
        models.Model.save(reading)  # trusted historical-writer simulation
        item = CrmItem.objects.create(organization=self.org, pool=self.pool, client=self.client_record, direction='service', title='Legacy source')
        task = ServiceTask.objects.create(organization=self.org, pool=self.pool, client=self.client_record, water_reading=reading,
            crm_item=item, title='Legacy source', task_type='supply_request', start_date=timezone.now().date(), created_by=self.user,
            primary_responsible=self.user, visibility='private')
        before = CrmObjectNeed.objects.count()
        self.client.force_login(self.user)
        row = self.client.get(reverse('crm_object_need_detail', args=[task.pk]), {'org_id':self.org.pk}).json()['need']
        self.assertTrue(row['legacy'])
        self.assertEqual(row['source_revision'], 0)
        self.assertEqual(CrmObjectNeed.objects.count(), before)
        payload = {key: row[key] for key in ('source_revision','source_hash','review_revision','task_version')}
        payload.update(task_id=task.pk, operation_key='legacy-review', positions=self.payload()['positions'])
        first = self.execute(payload)
        self.assertEqual(self.execute(payload), first)
        self.assertEqual(first['source_revision'], 1)
        self.assertEqual(CrmObjectNeed.objects.count(), before + 1)

    def test_legacy_service_waiting_and_sent_stages_freeze_task_and_item(self):
        for stage in (CrmItem.STAGE_SERVICE_WAITING_SUPPLIER, CrmItem.STAGE_SERVICE_WAITING_STOCK, CrmItem.STAGE_SERVICE_SENT_TO_POOL):
            CrmItem.objects.filter(pk=self.task.crm_item_id).update(stage=stage)
            before_task = ServiceTask.objects.values().get(pk=self.task.pk)
            before_item = CrmItem.objects.values().get(pk=self.task.crm_item_id)
            self.reading.required_materials = 'Changed for ' + stage
            self.reading.save()
            self.assertEqual(before_task, ServiceTask.objects.values().get(pk=self.task.pk))
            self.assertEqual(before_item, CrmItem.objects.values().get(pk=self.task.crm_item_id))
            with self.assertRaises(PermissionDenied): self.execute(operation_key='review-' + stage)

    def test_queryset_source_writes_are_rejected_and_partial_save_uses_persisted_fields(self):
        with self.assertRaises(ValidationError):
            WaterReading.objects.filter(pk=self.reading.pk).update(required_materials='Unsupported')
        self.reading.added_by = self.customer  # stale omitted field must not affect generated text
        self.reading.required_materials = 'Partial source change'
        self.reading._source_actor_id = self.user.pk
        self.reading.save(update_fields=['required_materials'])
        self.task.refresh_from_db()
        self.assertIn('need-manager', self.task.description)
        self.assertNotIn('need-client', self.task.description)
        self.assertEqual(self.need.sources.last().actor_id, self.user.pk)


class DeferredSourcePushTests(NeedFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.reading.added_by = self.customer
        self.reading.save(update_fields=['added_by'])

    def test_source_push_rechecks_hr_and_current_task_authority_after_commit(self):
        from pool_service.services.notifications import notify_task_assignment
        with patch('pool_service.services.notifications.send_push_to_users') as push:
            with self.captureOnCommitCallbacks(execute=True):
                notify_task_assignment(self.task, [self.user], added_by=self.customer, defer_push=True)
                self.task.responsibles.clear()
                self.task.created_by = self.customer
                self.task.primary_responsible = self.customer
                self.task.save()
            push.assert_not_called()
        self.task.responsibles.add(self.user)
        employee = Employee.objects.create(organization=self.org, user=self.user, display_name='Synthetic push recipient')
        with patch('pool_service.services.notifications.send_push_to_users') as push:
            with self.captureOnCommitCallbacks(execute=True):
                notify_task_assignment(self.task, [self.user], added_by=self.customer, defer_push=True)
                Employee.objects.filter(pk=employee.pk).update(is_active=False)
            push.assert_not_called()

    def test_source_push_reconstructs_current_source_text_without_snapshot(self):
        from pool_service.services.notifications import notify_task_assignment
        with patch('pool_service.services.notifications.send_push_to_users') as push:
            with self.captureOnCommitCallbacks(execute=True):
                notify_task_assignment(self.task, [self.user], added_by=self.customer, defer_push=True)
                self.reading.required_materials = 'Current hose composition'
                self.reading.save()
            push.assert_called_once()
            self.assertIn('Current hose composition', push.call_args.kwargs['message'])
            self.assertNotIn('Chlorine and pump', push.call_args.kwargs['message'])


class PlainReadingCompatibilityTests(NeedFixture, TestCase):
    def test_new_plain_reading_keeps_existing_non_source_graph_contract(self):
        client = Client.objects.create(name='Legacy unscoped customer')
        pool = Pool.objects.create(organization=self.org, client=client)
        reading = WaterReading.objects.create(pool=pool, added_by=self.user, date=timezone.now(), ph=7.3)
        self.assertFalse(CrmObjectNeed.objects.filter(reading=reading).exists())
        self.assertFalse(ServiceTask.objects.filter(water_reading=reading).exists())
        reading.comment = 'Only a measurement update'
        reading.save()
        self.assertEqual(WaterReading.objects.get(pk=reading.pk).comment, 'Only a measurement update')
