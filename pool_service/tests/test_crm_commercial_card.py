"""Bounded own commercial fields and explicit, default-off negative outcomes."""
from decimal import Decimal
import json
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from pool_service.client_crm_models import ClientOwnedObject
from pool_service.crm_commercial_models import CrmAccess, CrmCommand, CrmDeal, CrmEvent
from pool_service.models import Client, Organization, OrganizationAccess, Pool, ServiceTask, ServiceTaskChange
from pool_service.services.crm_commercial_commands import (
    CommercialConflict, close_commercial_deal_negative, execute_commercial_command,
)
from pool_service.services.task_feedback import version_for
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture


class CommercialCardTests(PilotFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.result = self.create()
        self.deal = CrmDeal.objects.get(pk=self.result['deal_id'])
        self.task = ServiceTask.objects.get(pk=self.result['task_id'])
        self.pool = Pool.objects.create(organization=self.org, client=self.client_record, address='Synthetic pool address')
        self.object = ClientOwnedObject.objects.create(client=self.client_record, object_type='hammam', name='Synthetic hammam', address='Synthetic object address')

    def edit(self, changes, **kwargs):
        values = dict(actor=self.user, organization=self.org, operation_key=str(uuid4()), command_type='deal.edit',
            subject_id=self.deal.pk, expected_revision=self.deal.revision, changes=changes)
        values.update(kwargs)
        return execute_commercial_command(**values)

    def close(self, **kwargs):
        values = dict(actor=self.user, organization=self.org, operation_key='card-negative-close',
            subject_id=self.deal.pk, expected_revision=self.deal.revision, outcome='lost', reason='Synthetic refusal',
            expected_task_version=version_for(self.task))
        values.update(kwargs)
        return close_commercial_deal_negative(**values)

    def enable_close(self):
        self.org.crm_close = True
        self.org.save(update_fields=['crm_close'])

    def test_full_card_create_and_edit_keep_null_distinct_from_zero_and_marker_history(self):
        card = dict(work_subject='Build synthetic hammam', specialization='hammam', fulfillment_kind='construction',
            object_kind='planned', planned_object_name='Planned synthetic hammam', planned_object_address='Planned address',
            amount='0.00', currency='RUB', amount_basis='indicative', expected_decision_at=self.due.isoformat())
        created = self.create(operation_key='full-create', card_fields=card)
        deal = CrmDeal.objects.get(pk=created['deal_id'])
        self.assertEqual((deal.amount, deal.expected_decision_at, deal.object_kind), (Decimal('0.00'), self.due, 'planned'))
        self.assertEqual((Pool.objects.count(), ClientOwnedObject.objects.count()), (1, 1))
        self.assertEqual(self.create(operation_key='full-create', card_fields=card), created)
        saved = self.edit(card, operation_key='full-edit')
        self.deal.refresh_from_db()
        event = CrmEvent.objects.get(pk=saved['event_id'])
        self.assertEqual(event.safe_diff['amount'], {'before': None, 'after': '0.00'})
        self.assertNotIn('Planned address', str(event.safe_diff))
        self.assertNotIn('Build synthetic', str(event.safe_diff))
        self.assertEqual(self.edit(card, operation_key='full-edit', expected_revision=1), saved)
        self.edit({'amount': None, 'amount_basis': 'unknown'})
        self.deal.refresh_from_db()
        self.assertIsNone(self.deal.amount)

    def test_amount_currency_datetime_and_text_validation_is_atomic(self):
        for amount in (1.2, True, 'NaN', 'Infinity', '-1', '1.001', '10000000000000000', '1e999', 'x'):
            with self.subTest(amount=amount), self.assertRaises(ValidationError):
                self.edit({'amount': amount, 'currency': 'RUB', 'amount_basis': 'indicative'})
        for changes in ({'amount': '1', 'currency': 'rub', 'amount_basis': 'agreed'},
                {'amount': '1', 'currency': '', 'amount_basis': 'agreed'}, {'amount': '1', 'currency': 'RUB', 'amount_basis': 'unknown'},
                {'expected_decision_at': '2026-10-27T11:00:00'}, {'expected_decision_at': 'not a date'},
                {'work_subject': 'x' * 2001}, {'specialization': 'x' * 41}, {'fulfillment_kind': 'payment'}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.edit(changes)
        self.deal.refresh_from_db()
        self.assertEqual(self.deal.revision, 1)
        self.assertEqual((CrmCommand.objects.count(), CrmEvent.objects.count()), (1, 1))

    def test_object_shapes_foreign_clients_orgs_and_deleted_selection_fail(self):
        other = Client.objects.create(organization=self.org, name='Other synthetic client')
        foreign = Organization.objects.create(name='Foreign synthetic org')
        bad_pool = Pool.objects.create(client=other, organization=foreign, address='Foreign synthetic object')
        bad_owned = ClientOwnedObject.objects.create(client=other, object_type='hammam', name='Other owned')
        for values in ({'object_kind': 'pool'}, {'object_kind': 'planned'}, {'object_kind': 'none', 'pool_id': self.pool.pk},
                {'object_kind': 'pool', 'pool_id': bad_pool.pk}, {'object_kind': 'client_object', 'client_object_id': bad_owned.pk},
                {'object_kind': 'pool', 'pool_id': self.pool.pk, 'planned_object_name': 'Unexpected'}):
            with self.subTest(values=values), self.assertRaises((PermissionDenied, ValidationError)):
                self.edit(values)
        self.pool.is_deleted = True
        self.pool.save(update_fields=['is_deleted'])
        with self.assertRaises(PermissionDenied):
            self.edit({'object_kind': 'pool', 'pool_id': self.pool.pk})
        self.assertEqual(CrmEvent.objects.count(), 1)

    def test_current_object_scope_and_permissions_rechecked_on_edit_and_create_replay(self):
        values = {'object_kind': 'client_object', 'client_object_id': self.object.pk}
        saved = self.edit(values, operation_key='select-object')
        self.deal.refresh_from_db()
        other = Client.objects.create(organization=self.org, name='Reparent target')
        self.object.client = other
        self.object.save(update_fields=['client'])
        with self.assertRaises(ValidationError):
            self.edit(values, operation_key='select-object', expected_revision=1)
        with self.assertRaises(ValidationError):
            self.edit({'title': 'Cannot bypass invalid graph'})
        self.object.client = self.client_record
        self.object.save(update_fields=['client'])
        self.member.role = 'accountant'
        self.member.save()
        self.grant.view_team_deals = True
        self.grant.save()
        with self.assertRaises(PermissionDenied):
            self.edit(values, operation_key='select-object', expected_revision=1)
        self.member.role = 'manager'; self.member.save()
        created = self.create(operation_key='object-create', card_fields={'object_kind': 'pool', 'pool_id': self.pool.pk})
        self.pool.is_deleted = True; self.pool.save(update_fields=['is_deleted'])
        with self.assertRaises(PermissionDenied):
            self.create(operation_key='object-create', card_fields={'object_kind': 'pool', 'pool_id': self.pool.pk})
        self.assertEqual(CrmDeal.objects.get(pk=created['deal_id']).revision, 1)

    def test_merged_client_cannot_be_edited_or_replayed_as_an_active_identity(self):
        from pool_service.client_crm_models import ClientCRMProfile
        values = {'object_kind': 'pool', 'pool_id': self.pool.pk}
        self.edit(values, operation_key='before-client-merge')
        target = Client.objects.create(organization=self.org, name='Synthetic merge target')
        ClientCRMProfile.objects.create(client=self.client_record, merged_into=target)
        with self.assertRaises(PermissionDenied):
            self.edit(values, operation_key='before-client-merge', expected_revision=1)
        self.enable_close()
        with self.assertRaises(ValidationError):
            self.close()

    def test_shared_objects_are_protected_and_never_edited_by_deal_fields(self):
        self.edit({'object_kind': 'client_object', 'client_object_id': self.object.pk})
        with self.assertRaises(ProtectedError):
            self.object.delete()
        self.object.refresh_from_db()
        self.assertEqual(self.object.address, 'Synthetic object address')
        with self.assertRaises(ValidationError):
            self.edit({'client_object': {'address': 'Mutation'}})

    def test_default_off_close_gate_and_revoked_flag_on_replay(self):
        with self.assertRaises(PermissionDenied):
            self.close()
        self.enable_close()
        result = self.close()
        # Exact replay uses original version from before the cancellation.
        original = result.copy()
        self.org.crm_close = False; self.org.save(update_fields=['crm_close'])
        with self.assertRaises(PermissionDenied):
            self.close()
        self.assertEqual(CrmEvent.objects.count(), 2)
        self.org.crm_close = True; self.org.save(update_fields=['crm_close'])
        self.assertEqual(self.close(), original)

    def test_negative_outcome_cancels_only_its_reminder_keeps_links_and_replays_once(self):
        self.enable_close()
        before_version = version_for(self.task)
        saved = self.close(expected_task_version=before_version)
        self.task.refresh_from_db(); self.deal.refresh_from_db()
        self.assertEqual((self.deal.outcome, self.deal.close_reason, self.deal.stage.code), ('lost', 'Synthetic refusal', 'need'))
        self.assertIsNotNone(self.deal.closed_at)
        self.assertEqual(self.task.status, ServiceTask.STATUS_CANCELLED)
        self.assertTrue(self.task.is_archived)
        self.assertIsNone(self.task.completed_at)
        self.assertEqual(self.deal.next_action.task_link.task_id, self.task.pk)
        self.assertEqual(ServiceTaskChange.objects.filter(field_name='crm_deal_negative_close').count(), 1)
        self.assertEqual(self.close(expected_revision=1, expected_task_version=before_version), saved)
        with self.assertRaises(CommercialConflict):
            self.close(expected_revision=1, expected_task_version=before_version, reason='Changed refusal')
        with self.assertRaises(ValidationError):
            self.close(operation_key='second-negative', expected_task_version=version_for(self.task))
        with self.assertRaises(ValidationError):
            self.edit({'title': 'Closed edit'})

    def test_negative_close_requires_revision_task_version_reason_current_rights_and_preparatory_stage(self):
        self.enable_close()
        for values, exception in (({'expected_revision': 0}, CommercialConflict), ({'expected_task_version': None}, CommercialConflict),
                ({'outcome': 'won'}, ValidationError), ({'reason': ''}, ValidationError), ({'reason': 'x'*501}, ValidationError)):
            with self.subTest(values=values), self.assertRaises(exception):
                self.close(**values)
        for capability in ('transition_own_deals', 'manage_own_next_action'):
            setattr(self.grant, capability, False); self.grant.save()
            with self.assertRaises(PermissionDenied): self.close()
            setattr(self.grant, capability, True); self.grant.save()
        stranger = get_user_model().objects.create_user('synthetic-other-manager')
        OrganizationAccess.objects.create(organization=self.org, user=stranger, role='manager')
        CrmAccess.objects.create(organization=self.org, user=stranger, transition_own_deals=True, manage_own_next_action=True)
        with self.assertRaises(PermissionDenied): self.close(actor=stranger)
        self.deal.stage = self.pipeline.stages.get(code='completed'); self.deal.save()
        with self.assertRaises(ValidationError): self.close()
        self.assertEqual((CrmEvent.objects.count(), self.task.status), (1, ServiceTask.STATUS_NEW))

    def test_waiting_immutable_private_and_changed_task_cannot_be_cancelled(self):
        self.enable_close()
        self.task.status = ServiceTask.STATUS_WAITING; self.task.save()
        with self.assertRaises(ValidationError): self.close()
        self.task.status = ServiceTask.STATUS_NEW; self.task.is_editable = False; self.task.save()
        with self.assertRaises(ValidationError): self.close()
        self.task.is_editable = True; self.task.save()
        with patch('pool_service.services.call_privacy.private_source_task_ids', return_value={self.task.pk}):
            with self.assertRaises(PermissionDenied): self.close()
        version = version_for(self.task)
        self.task.title = 'Changed task'; self.task.save()
        with self.assertRaises(CommercialConflict): self.close(expected_task_version=version)
        self.deal.refresh_from_db(); self.assertEqual(self.deal.outcome, 'open')
        self.assertEqual(ServiceTaskChange.objects.filter(field_name='crm_deal_negative_close').count(), 0)

    def test_terminal_reminder_and_absent_legacy_reminder_supported_without_false_completion(self):
        self.enable_close()
        self.task.status = ServiceTask.STATUS_DONE; self.task.completed_at = timezone.now(); self.task.save()
        completed = self.task.completed_at
        self.close(outcome='cancelled')
        self.task.refresh_from_db()
        self.assertEqual(self.task.completed_at, completed)
        self.assertFalse(self.task.is_archived)
        legacy = CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline, stage=self.pipeline.stages.get(code='need'),
            manager=self.user, client=self.client_record, title='Legacy without pointer')
        self.close(subject_id=legacy.pk, operation_key='legacy-close', expected_revision=0, expected_task_version=None)
        legacy.refresh_from_db(); self.assertEqual(legacy.outcome, 'lost')

    def test_audit_failure_rolls_back_close_and_reminder(self):
        self.enable_close()
        before = self.state()
        with patch.object(CrmEvent, 'save', side_effect=RuntimeError('Synthetic journal failure')):
            with self.assertRaises(RuntimeError): self.close()
        self.assertEqual(before, self.state())

    def test_history_rejects_arbitrary_text_payloads_and_invalid_typed_values(self):
        for diff in ({'work_subject': {'before': '', 'after': 'Sensitive source body'}},
                {'amount': {'before': None, 'after': 'NaN'}}, {'currency': {'before': '', 'after': 'secret'}},
                {'object_kind': {'before': 'none', 'after': 'private-body'}},
                {'expected_decision_at': {'before': None, 'after': '2026-10-27T10:00:00'}},
                {'work_subject_changed': {'before': False, 'after': 'body'}}):
            with self.subTest(diff=diff), self.assertRaises(ValidationError):
                CrmEvent.objects.create(organization=self.org, deal=self.deal, event_type='deal.edit', safe_diff=diff)


class CommercialCardHttpTests(PilotFixture, TestCase):
    def setUp(self):
        super().setUp()
        from datetime import timedelta
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=['paid_until'])
        self.client.force_login(self.user)
        self.result = self.create()
        self.pool = Pool.objects.create(organization=self.org, client=self.client_record, address='Searchable synthetic address')
        self.object = ClientOwnedObject.objects.create(client=self.client_record, object_type='hammam', name='Searchable hammam')

    def url(self, name, *args):
        return reverse(name, args=args) + '?org_id=' + str(self.org.pk)

    def post(self, kind, changes, *, revision=1, key=None):
        return self.client.post(self.url('crm_live_command'), json.dumps({'operation_key': key or str(uuid4()),
            'command_type': kind, 'subject_id': self.result['deal_id'], 'expected_revision': revision, 'changes': changes}),
            content_type='application/json')

    def rows(self, filters=''):
        return self.client.get(self.url('crm_live_data') + filters).json()['deals']

    def test_object_lookup_is_bounded_client_scoped_and_requires_own_create_or_edit(self):
        other = Client.objects.create(organization=self.org, name='Other lookup client')
        Pool.objects.create(organization=self.org, client=other, address='Hidden other address')
        ClientOwnedObject.objects.create(client=other, object_type='hammam', name='Hidden other name')
        url = self.url('crm_live_objects') + '&client_id=' + str(self.client_record.pk)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        rows = response.json()['objects']
        self.assertEqual({(row['kind'], row['id']) for row in rows}, {('pool', self.pool.pk), ('client_object', self.object.pk)})
        self.assertNotContains(response, 'Hidden other')
        self.assertTrue(all(set(row) == {'kind', 'id', 'name', 'address'} for row in rows))
        self.assertEqual(self.client.get(url + '&q=notfound').json()['objects'], [])
        for index in range(22):
            ClientOwnedObject.objects.create(client=self.client_record, object_type='hammam', name='Bounded ' + str(index))
        self.assertLessEqual(len(self.client.get(url).json()['objects']), 40)
        self.grant.create_own_deals = False; self.grant.save()
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.get(url + '&deal_id=' + str(self.result['deal_id'])).status_code, 200)
        self.grant.edit_own_deals = False; self.grant.save()
        self.assertEqual(self.client.get(url + '&deal_id=' + str(self.result['deal_id'])).status_code, 403)

    def test_existing_object_policy_is_additional_to_team_deal_read(self):
        self.assertEqual(self.post('deal.edit', {'object_kind': 'client_object', 'client_object_id': self.object.pk}).status_code, 200)
        self.member.role = 'accountant'; self.member.save()
        self.grant.view_team_deals = True; self.grant.save()
        # Another operational membership avoids the independent finance-only redirect;
        # object authority must still come from the selected organization.
        second_org = Organization.objects.create(name='Other operational membership')
        OrganizationAccess.objects.create(organization=second_org, user=self.user, role='manager')
        row = self.client.get(self.url('crm_live_detail', self.result['deal_id'])).json()['deal']
        self.assertEqual(row['object'], {'restricted': True})
        self.assertFalse(row['can_edit'])
        self.assertEqual(self.rows('&q=Searchable'), [])
        self.assertEqual(self.client.get(self.url('crm_live_objects') + '&client_id=' + str(self.client_record.pk)).status_code, 403)
        self.assertEqual(self.post('deal.edit', {'title': 'Denied'}, revision=2).status_code, 403)

    def test_reparented_or_deleted_objects_never_leak_through_detail_or_search(self):
        self.assertEqual(self.post('deal.edit', {'object_kind': 'pool', 'pool_id': self.pool.pk}).status_code, 200)
        self.assertEqual(len(self.rows('&q=Searchable')), 1)
        other = Client.objects.create(organization=self.org, name='Reparent target')
        self.pool.client = other; self.pool.save(update_fields=['client'])
        response = self.client.get(self.url('crm_live_detail', self.result['deal_id']))
        self.assertEqual(response.json()['deal']['object'], {'restricted': True})
        self.assertNotContains(response, 'Searchable synthetic address')
        self.assertEqual(self.rows('&q=Searchable'), [])
        self.pool.client = self.client_record; self.pool.is_deleted = True; self.pool.save(update_fields=['client', 'is_deleted'])
        self.assertEqual(self.rows('&q=Searchable'), [])
        foreign = Organization.objects.create(name='Foreign reparent')
        self.pool.organization = foreign; self.pool.save(update_fields=['organization'])
        self.assertEqual(self.rows('&q=Searchable'), [])

    def test_full_card_save_filters_and_closed_history_without_changing_flags(self):
        changes = {'work_subject': 'Synthetic installation', 'specialization': 'hammam', 'fulfillment_kind': 'construction',
            'object_kind': 'planned', 'planned_object_name': 'Planned install', 'planned_object_address': 'Planned location',
            'amount': '0', 'currency': 'RUB', 'amount_basis': 'agreed', 'expected_decision_at': '2026-10-27T10:00:00+07:00'}
        response = self.post('deal.edit', changes)
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()['deal']
        self.assertEqual(row['amount'], '0.00')
        self.assertFalse(row['can_close_negative'])
        self.assertEqual(len(self.rows('&object=planned&specialization=hammam&decision_from=2026-10-27&decision_to=2026-10-27')), 1)
        for filters in ('&object=none', '&specialization=pool', '&decision_from=2026-10-28', '&decision_to=2026-10-26'):
            self.assertEqual(self.rows(filters), [])
        for invalid in ('&stage=9999', '&outcome=invalid', '&object=invalid', '&decision_from=nonsense'):
            self.assertEqual(self.client.get(self.url('crm_live_data') + invalid).status_code, 400)
        close = {'outcome': 'cancelled', 'reason': 'Synthetic cancellation', 'expected_task_version': row['next_action']['version']}
        self.assertEqual(self.post('deal.close_negative', close, revision=2).status_code, 403)
        self.org.crm_close = True; self.org.save(update_fields=['crm_close'])
        saved = self.post('deal.close_negative', close, revision=2)
        self.assertEqual(saved.status_code, 200, saved.content)
        self.assertFalse(saved.json()['deal']['can_edit'])
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.rows('&outcome=cancelled')), 1)
        self.assertEqual(len(self.rows('&outcome=all')), 1)
        detail = self.client.get(self.url('crm_live_detail', self.result['deal_id'])).json()
        self.assertEqual(detail['events'][0]['reason'], close['reason'])
        self.org.crm_close = False; self.org.save(update_fields=['crm_close'])
        self.assertEqual(self.client.get(self.url('crm_live_detail', self.result['deal_id'])).status_code, 200)

    def test_client_owned_delete_compatibility_returns_message_instead_of_server_error(self):
        self.post('deal.edit', {'object_kind': 'client_object', 'client_object_id': self.object.pk})
        self.member.role = 'admin'; self.member.save()
        response = self.client.post(reverse('client_detail', args=[self.client_record.pk]),
            {'action': 'delete_owned_object', 'object_id': self.object.pk})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(ClientOwnedObject.objects.filter(pk=self.object.pk).exists())


from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from django.db import close_old_connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from pool_service.services.crm_commercial_commands import set_deal_next_action


@skipUnlessDBFeature('has_select_for_update')
class CommercialCardRaceTests(PilotFixture, TransactionTestCase):
    def test_close_and_next_step_serialize_on_current_revision_and_task(self):
        self.org.crm_close = True
        self.org.save(update_fields=['crm_close'])
        result = self.create()
        task = ServiceTask.objects.get(pk=result['task_id'])
        version = version_for(task)
        barrier = Barrier(2)

        def run(kind):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                common = dict(actor=self.user, organization=self.org, operation_key='card-race-' + kind,
                    subject_id=result['deal_id'], expected_revision=result['revision'], expected_task_version=version)
                if kind == 'close':
                    close_commercial_deal_negative(**common, outcome='lost', reason='Synthetic race refusal')
                else:
                    set_deal_next_action(**common, title='New synthetic action', due_at=self.due)
                return kind
            except CommercialConflict:
                return 'conflict'
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(run, ('close', 'next')))
        self.assertEqual(outcomes.count('conflict'), 1)
        deal = CrmDeal.objects.get(pk=result['deal_id'])
        task.refresh_from_db()
        self.assertEqual(deal.revision, 2)
        self.assertEqual(CrmCommand.objects.count(), 2)
        self.assertEqual(CrmEvent.objects.count(), 2)
        self.assertIsNone(task.completed_at)
        if 'close' in outcomes:
            self.assertEqual((deal.outcome, task.status, task.is_archived), ('lost', ServiceTask.STATUS_CANCELLED, True))
        else:
            self.assertEqual((deal.outcome, task.title, task.is_archived), ('open', 'New synthetic action', False))


class CommercialCardPaginationTests(PilotFixture, TestCase):
    def test_second_page_contains_old_matching_cards_in_both_views_with_filters(self):
        self.client.force_login(self.user)
        stage = self.pipeline.stages.get(code='need')
        ids = []
        for index in range(51):
            row = CrmDeal.objects.create(organization=self.org, client=self.client_record, manager=self.user,
                pipeline=self.pipeline, stage=stage, title='Synthetic paginated ' + str(index),
                object_kind='planned', planned_object_name='Target planned', specialization='hammam', outcome='open')
            ids.append(row.pk)
        CrmDeal.objects.create(organization=self.org, client=self.client_record, manager=self.user,
            pipeline=self.pipeline, stage=stage, title='Excluded by filter', specialization='pool')
        url = reverse('crm_live_data') + '?org_id=' + str(self.org.pk) + '&specialization=hammam&object=planned&owner=me'
        first = self.client.get(url).json()
        self.assertEqual(len(first['deals']), 50)
        self.assertTrue(first['has_more'])
        self.assertFalse(first['has_previous'])
        for view in ('table', 'kanban'):
            second = self.client.get(url + '&page=2&view=' + view).json()
            self.assertEqual([row['id'] for row in second['deals']], [ids[0]])
            self.assertTrue(second['has_previous'])
            self.assertFalse(second['has_more'])
            self.assertEqual(second['page'], 2)
            self.assertFalse({row['id'] for row in first['deals']} & {row['id'] for row in second['deals']})
        self.assertEqual(self.client.get(url + '&page=0').status_code, 400)
        self.assertEqual(self.client.get(url + '&page=10001').status_code, 400)


class CommercialAttentionTests(PilotFixture, TestCase):
    def setUp(self):
        super().setUp()
        from datetime import timedelta
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save(update_fields=['paid_until'])
        self.client.force_login(self.user)

    def url(self, attention='all'):
        return reverse('crm_live_data') + '?org_id=' + str(self.org.pk) + '&attention=' + attention

    def deal_task(self, index, *, due=None):
        from pool_service.crm_commercial_models import CrmDealNextAction, CrmDealTaskLink
        deal = CrmDeal.objects.create(organization=self.org, client=self.client_record, manager=self.user,
            pipeline=self.pipeline, stage=self.pipeline.stages.get(code='need'), title='Synthetic attention ' + str(index))
        task = ServiceTask.objects.create(organization=self.org, client=self.client_record, created_by=self.user,
            title='Synthetic task ' + str(index), start_date=self.due.date(), due_at=due or self.due,
            source_type=ServiceTask.SOURCE_MANAGER, task_type=ServiceTask.TYPE_CRM_FOLLOWUP)
        link = CrmDealTaskLink.objects.create(organization=self.org, deal=deal, task=task, created_by=self.user)
        CrmDealNextAction.objects.create(organization=self.org, deal=deal, task_link=link)
        return deal, task

    def test_oldest_overdue_or_missing_is_selected_before_fifty_row_pagination(self):
        from datetime import timedelta
        now = timezone.now()
        oldest, old_task = self.deal_task(0, due=now - timedelta(days=1))
        for index in range(1, 51):
            self.deal_task(index, due=now + timedelta(days=1))
        overdue = self.client.get(self.url('overdue')).json()
        self.assertEqual([row['id'] for row in overdue['deals']], [oldest.pk])
        self.assertEqual(overdue['attention_total'], 1)
        self.assertFalse(overdue['has_more'])
        self.assertEqual(overdue['page'], 1)
        old_task.status = ServiceTask.STATUS_CANCELLED
        old_task.save(update_fields=['status'])
        missing = self.client.get(self.url('missing')).json()
        self.assertEqual([row['id'] for row in missing['deals']], [oldest.pk])
        self.assertEqual(missing['attention_total'], 1)
        oldest.outcome = 'cancelled'; oldest.close_reason = 'Synthetic closed'; oldest.closed_at = now; oldest.save()
        history = self.client.get(self.url('missing') + '&outcome=all').json()
        self.assertEqual(history['deals'], [])
        self.assertEqual(history['attention_total'], 0)

    def test_hidden_private_invalid_and_waiting_tasks_do_not_reveal_timing_or_missing(self):
        from datetime import timedelta
        from pool_service.call_processing_models import CallPrivateNumber
        from pool_service.communication_models import PhoneCall
        now = timezone.now()
        hidden, task = self.deal_task(1, due=now - timedelta(days=1))
        call = PhoneCall.objects.create(organization=self.org, employee=self.user, external_id='attention-private',
            phone_number='8 (913) 555-01-23', direction='in', result='answered', started_at=now)
        CallPrivateNumber.objects.create(organization=self.org, owner=self.user, label='Synthetic private', phone_key='9135550123')
        task.payload_json = {'source_call_id': call.pk}; task.save(update_fields=['payload_json'])
        invalid, invalid_task = self.deal_task(2, due=now - timedelta(days=1))
        invalid_task.client = Client.objects.create(organization=self.org, name='Other task client')
        invalid_task.save(update_fields=['client'])
        waiting, waiting_task = self.deal_task(3, due=now - timedelta(days=1))
        waiting_task.status = ServiceTask.STATUS_WAITING; waiting_task.end_date = None
        waiting_task.payload_json = {'task_feedback': {'mode': 'waiting'}}
        waiting_task.save(update_fields=['status', 'end_date', 'payload_json'])
        unrelated, unrelated_task = self.deal_task(4, due=now - timedelta(days=1))
        other_user = get_user_model().objects.create_user('attention-hidden-author')
        unrelated_task.created_by = other_user; unrelated_task.primary_responsible = self.user
        unrelated_task.save(update_fields=['created_by', 'primary_responsible'])
        for attention in ('overdue', 'missing'):
            rows = self.client.get(self.url(attention)).json()
            self.assertEqual(rows['deals'], [])
            self.assertEqual(rows['attention_total'], 0)
        task.status = unrelated_task.status = ServiceTask.STATUS_CANCELLED
        task.save(update_fields=['status']); unrelated_task.save(update_fields=['status'])
        self.assertEqual(self.client.get(self.url('missing')).json()['attention_total'], 0)
        # A later explicit end date overrides the waiting marker, as waiting_control does.
        waiting_task.end_date = now.date(); waiting_task.save(update_fields=['end_date'])
        self.assertEqual([row['id'] for row in self.client.get(self.url('overdue')).json()['deals']], [waiting.pk])

    def test_date_only_deadlines_use_barnaul_day_and_absent_pointer_is_missing(self):
        from datetime import timedelta
        from zoneinfo import ZoneInfo
        today = timezone.now().astimezone(ZoneInfo('Asia/Barnaul')).date()
        old, old_task = self.deal_task(1)
        current, current_task = self.deal_task(2)
        for task, day in ((old_task, today - timedelta(days=1)), (current_task, today)):
            task.due_at = None; task.start_date = day; task.end_date = None
            task.save(update_fields=['due_at', 'start_date', 'end_date'])
        absent = CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline, stage=self.pipeline.stages.get(code='need'),
            manager=self.user, client=self.client_record, title='Synthetic no pointer')
        self.assertEqual([row['id'] for row in self.client.get(self.url('overdue')).json()['deals']], [old.pk])
        self.assertEqual([row['id'] for row in self.client.get(self.url('missing')).json()['deals']], [absent.pk])
        self.assertEqual(self.client.get(self.url('bad')).status_code, 400)

    def test_thousand_candidate_boundary_is_explicit_and_does_not_limit_normal_workspace(self):
        stage = self.pipeline.stages.get(code='need')
        for index in range(1000):
            CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline, stage=stage,
                client=self.client_record, manager=self.user, title='Synthetic bounded attention ' + str(index))
        selected = self.client.get(self.url('missing'))
        self.assertEqual(selected.status_code, 200, selected.content)
        self.assertEqual(selected.json()['attention_total'], 1000)
        self.assertEqual(len(selected.json()['deals']), 50)
        CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline, stage=stage,
            client=self.client_record, manager=self.user, title='Synthetic sentinel attention')
        with patch('pool_service.crm_live_views.readable_tasks') as reader:
            refused = self.client.get(self.url('overdue'))
            self.assertEqual(refused.status_code, 400)
            self.assertIn('Уточните обычные фильтры', refused.json()['error'])
            reader.assert_not_called()
        normal = self.client.get(self.url())
        self.assertEqual(normal.status_code, 200)
        self.assertEqual(len(normal.json()['deals']), 50)
        self.assertIsNone(normal.json()['attention_total'])
        bookmarked = self.client.get(reverse('crm_live_workspace') + '?org_id=' + str(self.org.pk) + '&attention=overdue')
        self.assertEqual(bookmarked.status_code, 200)
        boot = bookmarked.context['crm_bootstrap']
        self.assertEqual(boot['attention'], 'all')
        self.assertEqual(len(boot['deals']), 50)
        self.assertIn('1000', boot['initial_notice'])
        # Narrowing a business filter selects an exact subset without partial results.
        narrowed = self.client.get(self.url('missing') + '&q=sentinel')
        self.assertEqual(narrowed.status_code, 200)
        self.assertEqual(narrowed.json()['attention_total'], 1)

    def test_malformed_saved_html_filters_recover_without_weakening_access_or_not_found(self):
        workspace = reverse('crm_live_workspace') + '?org_id=' + str(self.org.pk)
        for invalid in ('&page=bad', '&page=0', '&outcome=bad'):
            response = self.client.get(workspace + invalid)
            self.assertEqual(response.status_code, 200)
            boot = response.context['crm_bootstrap']
            self.assertEqual((boot['page'], boot['outcome'], boot['attention']), (1, 'open', 'all'))
            self.assertTrue(boot['initial_notice'])
        self.assertEqual(self.client.get(workspace + '&pipeline=999999').status_code, 404)
        self.org.crm_v2_read = False; self.org.save(update_fields=['crm_v2_read'])
        self.assertEqual(self.client.get(workspace + '&outcome=bad').status_code, 403)
