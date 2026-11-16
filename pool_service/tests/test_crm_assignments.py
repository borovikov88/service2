"""Synthetic responsibility changes, current authority, replay and task boundaries."""
import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, TransactionTestCase, Client as HttpClient
from django.urls import reverse
from django.utils import timezone

from pool_service.crm_commercial_models import CrmAccess, CrmCommand, CrmDeal, CrmEvent, CrmLead
from pool_service.models import Employee, Notification, Organization, OrganizationAccess, ServiceTask
from pool_service.services.crm_assignments import assignment_task_data, execute_assignment, responsible_candidates
from pool_service.services.crm_commercial_commands import CommercialConflict
from pool_service.services.crm_lead_commands import execute_lead_command
from pool_service.services.task_feedback import version_for
from pool_service.tests.test_crm_live_pilot_commands import PilotFixture
from pool_service.tests.test_crm_inbox import Fixture as InboxFixture


class CrmAssignmentTests(PilotFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.paid_until = timezone.now() + timedelta(days=10)
        self.org.save(update_fields=['paid_until'])
        for name in ('view_leads', 'create_own_leads', 'edit_own_leads', 'take_leads',
                     'qualify_own_leads', 'transition_own_leads', 'view_team_deals'):
            setattr(self.grant, name, True)
        self.grant.save()
        self.recipient = get_user_model().objects.create_user('synthetic-new-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.recipient, role='manager')
        self.recipient_employee = Employee.objects.create(organization=self.org, user=self.recipient, display_name='Synthetic recipient')
        self.recipient_grant = CrmAccess.objects.create(organization=self.org, user=self.recipient,
            view_leads=True, edit_own_leads=True, edit_own_deals=True, view_team_deals=True,
            transition_own_leads=True, transition_own_deals=True, create_own_leads=True,
            create_own_deals=True, manage_own_next_action=True, qualify_own_leads=True)
        self.http = HttpClient()
        self.http.force_login(self.user)

    def lead_create(self, *, key='assignment-lead-create', responsible_id=None):
        changes = {'title': 'Synthetic lead', 'need': 'Synthetic need',
            'client_id': self.client_record.pk,
            'next_action': {'title': 'Synthetic next action', 'due_at': self.due.isoformat()}}
        if responsible_id is not None:
            changes['responsible_id'] = responsible_id
        return execute_lead_command(actor=self.user, organization=self.org, operation_key=key,
            command_type='lead.create', changes=changes)

    def assign(self, subject, **kwargs):
        kind = 'lead' if isinstance(subject, CrmLead) else 'deal'
        values = dict(actor=self.user, organization=self.org, operation_key=kind+'-assign',
            command_type=kind+'.assign', subject_id=subject.pk, expected_revision=subject.revision,
            changes={'responsible_id': self.recipient.pk})
        values.update(kwargs)
        return execute_assignment(**values)

    def notes(self, recipient=None):
        rows = Notification.objects.filter(kind='crm_assignment')
        return rows.filter(user=recipient) if recipient else rows

    def test_create_deal_for_recipient_assigns_new_task_and_replays_once(self):
        result = self.create(responsible_id=self.recipient.pk)
        deal = CrmDeal.objects.get(pk=result['deal_id'])
        task = ServiceTask.objects.get(pk=result['task_id'])
        self.assertEqual(deal.manager_id, self.recipient.pk)
        self.assertEqual((task.primary_responsible_id, task.created_by_id), (self.recipient.pk, self.user.pk))
        self.assertEqual(list(task.responsibles.values_list('pk', flat=True)), [self.recipient.pk])
        self.assertEqual(self.notes(self.recipient).count(), 1)
        self.assertEqual(self.create(responsible_id=self.recipient.pk), result)
        self.assertEqual(self.notes(self.recipient).count(), 1)

    def test_create_lead_for_recipient_assigns_new_task_and_replays_once(self):
        result = self.lead_create(responsible_id=self.recipient.pk)
        lead = CrmLead.objects.get(pk=result['lead_id'])
        task = lead.next_task
        self.assertEqual(lead.assignee_id, self.recipient.pk)
        self.assertEqual(task.primary_responsible_id, self.recipient.pk)
        self.assertEqual(list(task.responsibles.values_list('pk', flat=True)), [self.recipient.pk])
        self.assertEqual(result, self.lead_create(responsible_id=self.recipient.pk))
        self.assertEqual(self.notes(self.recipient).count(), 1)

    def test_deal_transfer_history_and_replay_preserve_existing_task(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        task = deal.next_action.task_link.task
        version = version_for(task)
        result = self.assign(deal)
        deal.refresh_from_db(); task.refresh_from_db()
        self.assertEqual(deal.manager_id, self.recipient.pk)
        self.assertEqual(task.primary_responsible_id, self.user.pk)
        self.assertEqual(version_for(task), version)
        self.assertEqual(self.assign(deal, expected_revision=1), result)
        self.assertEqual(self.notes(self.recipient).count(), 1)
        event = CrmEvent.objects.get(pk=result['event_id'])
        self.assertEqual(event.safe_diff['manager_id'], {'before': self.user.pk, 'after': self.recipient.pk})

    def test_lead_transfer_preserves_task_and_duplicate_command_does_not_notify_again(self):
        lead = CrmLead.objects.get(pk=self.lead_create()['lead_id'])
        task_id = lead.next_task_id
        result = self.assign(lead)
        lead.refresh_from_db()
        self.assertEqual(lead.assignee_id, self.recipient.pk)
        self.assertEqual(lead.next_task_id, task_id)
        self.assertEqual(lead.next_task.primary_responsible_id, self.user.pk)
        self.assertEqual(self.assign(lead, expected_revision=1), result)
        self.assertEqual(self.notes(self.recipient).count(), 1)

    def test_noop_stale_version_and_changed_replay_payload_leave_no_notification(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        before = self.notes().count()
        with self.assertRaises(ValidationError): self.assign(deal, changes={'responsible_id': self.user.pk})
        with self.assertRaises(CommercialConflict): self.assign(deal, expected_revision=0)
        self.assertEqual(self.notes().count(), before)
        self.assign(deal)
        with self.assertRaises(CommercialConflict): self.assign(deal, changes={'responsible_id': self.user.pk})
        self.assertEqual(self.notes().count(), before + 1)

    def test_manager_cannot_change_another_owner_but_explicit_crm_admin_can(self):
        deal = CrmDeal.objects.get(pk=self.create(responsible_id=self.recipient.pk)['deal_id'])
        with self.assertRaises(PermissionDenied): self.assign(deal, changes={'responsible_id': self.user.pk})
        self.member.role = 'admin'; self.member.save(update_fields=['role'])
        self.assign(deal, changes={'responsible_id': self.user.pk})
        deal.refresh_from_db(); self.assertEqual(deal.manager_id, self.user.pk)

    def test_admin_can_assign_unallocated_lead_without_inventing_task_or_changing_status(self):
        lead = CrmLead.objects.create(organization=self.org, title='Synthetic unallocated', status='new')
        with self.assertRaises(PermissionDenied): self.assign(lead)
        self.member.role = 'owner'; self.member.save(update_fields=['role'])
        self.assign(lead)
        lead.refresh_from_db()
        self.assertEqual((lead.assignee_id, lead.status, lead.next_task_id), (self.recipient.pk, 'new', None))
        self.assertEqual(self.notes(self.recipient).count(), 1)

    def test_recipient_needs_current_membership_active_employment_and_crm_grants(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        self.recipient.is_active = False; self.recipient.save(update_fields=['is_active'])
        with self.assertRaises(PermissionDenied): self.assign(deal)
        self.recipient.is_active = True; self.recipient.save(update_fields=['is_active'])
        self.recipient_employee.employment_status = Employee.STATUS_DISMISSED
        self.recipient_employee.save(update_fields=['employment_status'])
        with self.assertRaises(PermissionDenied): self.assign(deal)
        self.recipient_employee.employment_status = Employee.STATUS_EMPLOYED
        self.recipient_employee.save(update_fields=['employment_status'])
        self.recipient_grant.delete()
        with self.assertRaises(PermissionDenied): self.assign(deal)
        self.assertEqual(self.notes(self.recipient).count(), 0)

    def test_foreign_recipient_and_foreign_subject_are_rejected(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        OrganizationAccess.objects.filter(organization=self.org, user=self.recipient).delete()
        with self.assertRaises(PermissionDenied): self.assign(deal)
        foreign = Organization.objects.create(name='Foreign synthetic')
        with self.assertRaises(PermissionDenied): self.assign(deal, organization=foreign)

    def test_recipient_source_visibility_is_enforced_on_list_and_write(self):
        lead = CrmLead.objects.get(pk=self.lead_create()['lead_id'])
        from pool_service.services.crm_lead_access import can_read_lead
        def readable(user, organization, subject):
            return user.pk != self.recipient.pk and can_read_lead(user, organization, subject)
        with patch('pool_service.services.crm_assignments.can_read_lead', side_effect=readable):
            choices = responsible_candidates(actor=self.user, organization=self.org, kind='lead', subject_id=lead.pk)
            self.assertNotIn(self.recipient.pk, [row['id'] for row in choices['users']])
            with self.assertRaises(PermissionDenied): self.assign(lead)
        self.assertFalse(self.notes(self.recipient).exists())

    def test_current_actor_grant_is_rechecked_before_replay(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        self.assign(deal)
        self.grant.edit_own_deals = False; self.grant.save(update_fields=['edit_own_deals'])
        with self.assertRaises(PermissionDenied): self.assign(deal)
        self.assertEqual(self.notes(self.recipient).count(), 1)

    def test_closed_and_archived_cards_are_readonly_for_assignment(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        deal.is_archived = True; deal.save()
        with self.assertRaises(PermissionDenied): self.assign(deal)
        lead = CrmLead.objects.get(pk=self.lead_create()['lead_id'])
        lead.status = 'closed'; lead.result = 'no_deal'; lead.reason = 'Synthetic closed'; lead.save()
        with self.assertRaises(PermissionDenied): self.assign(lead)

    def test_conversion_selects_deal_manager_and_new_task_recipient(self):
        lead = CrmLead.objects.get(pk=self.lead_create()['lead_id'])
        changes = {'deals': [{'pipeline_id': self.pipeline.pk, 'title': 'Synthetic converted',
            'responsible_id': self.recipient.pk,
            'next_action': {'title': 'Synthetic conversion step', 'due_at': self.due.isoformat()}}]}
        params = dict(actor=self.user, organization=self.org, operation_key='selected-conversion',
            command_type='lead.qualify', subject_id=lead.pk, expected_revision=lead.revision, changes=changes)
        result = execute_lead_command(**params)
        deal = CrmDeal.objects.get(pk=result['deal_ids'][0])
        self.assertEqual(deal.manager_id, self.recipient.pk)
        self.assertEqual(deal.next_action.task_link.task.primary_responsible_id, self.recipient.pk)
        self.assertEqual(execute_lead_command(**params), result)
        self.assertEqual(self.notes(self.recipient).count(), 1)

    def test_candidates_get_does_not_write_and_assignment_requires_csrf(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        before = (CrmCommand.objects.count(), CrmEvent.objects.count(), self.notes().count())
        url = reverse('crm_assignment_candidates') + '?org_id=' + str(self.org.pk)
        response = self.http.get(url, {'kind': 'deal', 'subject_id': deal.pk, 'org_id': self.org.pk})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn(self.recipient.pk, [row['id'] for row in response.json()['users']])
        self.assertEqual(before, (CrmCommand.objects.count(), CrmEvent.objects.count(), self.notes().count()))
        client = HttpClient(enforce_csrf_checks=True); client.force_login(self.user)
        payload = {'operation_key': 'http-assignment', 'command_type': 'deal.assign', 'subject_id': deal.pk,
            'expected_revision': deal.revision, 'changes': {'responsible_id': self.recipient.pk}}
        self.assertEqual(client.post(reverse('crm_live_command')+'?org_id='+str(self.org.pk),
            json.dumps(payload), content_type='application/json').status_code, 403)
        response = self.http.post(reverse('crm_live_command')+'?org_id='+str(self.org.pk),
            json.dumps(payload), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['deal']['manager']['id'], self.recipient.pk)

    def test_notification_failure_rolls_back_assignment_and_history(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        before = (CrmCommand.objects.count(), CrmEvent.objects.count())
        with patch('pool_service.services.crm_assignment_notifications.notify_assignment', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError): self.assign(deal)
        deal.refresh_from_db(); self.assertEqual(deal.manager_id, self.user.pk)
        self.assertEqual(before, (CrmCommand.objects.count(), CrmEvent.objects.count()))

    def test_explicit_current_task_transfer_makes_new_lead_owner_fully_operational(self):
        lead = CrmLead.objects.get(pk=self.lead_create()['lead_id'])
        task = lead.next_task
        data = assignment_task_data(lead, self.user)
        self.assertTrue(data['can_transfer'])
        self.assign(lead, changes={'responsible_id': self.recipient.pk,
            'transfer_next_action': True, 'expected_task_version': data['version']})
        lead.refresh_from_db(); task.refresh_from_db()
        self.assertEqual(task.primary_responsible_id, self.recipient.pk)
        self.assertEqual(task.created_by_id, self.user.pk)
        self.assertEqual(list(task.responsibles.values_list('pk', flat=True)), [self.recipient.pk])
        result = execute_lead_command(actor=self.recipient, organization=self.org, operation_key='new-owner-qualifies',
            command_type='lead.qualify', subject_id=lead.pk, expected_revision=lead.revision,
            changes={'deals': [{'pipeline_id': self.pipeline.pk, 'title': 'Recipient conversion',
                'next_action': {'title': 'New deal step', 'due_at': self.due.isoformat()}}]})
        self.assertEqual(CrmDeal.objects.get(pk=result['deal_ids'][0]).manager_id, self.recipient.pk)

    def test_task_transfer_rejects_stale_shared_and_waiting_task_without_card_mutation(self):
        deal = CrmDeal.objects.get(pk=self.create()['deal_id'])
        task = deal.next_action.task_link.task
        changes = {'responsible_id': self.recipient.pk, 'transfer_next_action': True, 'expected_task_version': '0'*64}
        with self.assertRaises(CommercialConflict): self.assign(deal, changes=changes)
        changes['expected_task_version'] = version_for(task)
        task.responsibles.add(self.recipient)
        with self.assertRaises(ValidationError): self.assign(deal, changes=changes)
        task.responsibles.remove(self.recipient)
        task.status = ServiceTask.STATUS_WAITING; task.save(update_fields=['status'])
        changes['expected_task_version'] = version_for(task)
        with self.assertRaises(ValidationError): self.assign(deal, changes=changes)
        deal.refresh_from_db(); self.assertEqual(deal.manager_id, self.user.pk)
        self.assertFalse(self.notes(self.recipient).exists())

    def test_card_only_transfer_allows_private_task_free_title_need_edits(self):
        lead = CrmLead.objects.get(pk=self.lead_create()['lead_id'])
        task = lead.next_task
        before = version_for(task)
        self.assign(lead)
        lead.refresh_from_db()
        params = dict(actor=self.recipient, organization=self.org, operation_key='safe-card-edit',
            command_type='lead.edit', subject_id=lead.pk, expected_revision=lead.revision,
            changes={'title': 'New responsible title', 'need': 'Updated need'})
        result = execute_lead_command(**params)
        self.assertEqual(execute_lead_command(**params), result)
        task.refresh_from_db(); self.assertEqual(version_for(task), before)
        with self.assertRaises(PermissionDenied):
            execute_lead_command(actor=self.recipient, organization=self.org, operation_key='forbidden-task-edit',
                command_type='lead.edit', subject_id=lead.pk, expected_revision=result['revision'],
                changes={'next_action': {'title': 'Hidden task', 'due_at': self.due.isoformat()}, 'expected_task_version': before})

    def test_conversion_reuse_transfers_canonical_step_to_selected_deal_responsible(self):
        lead = CrmLead.objects.get(pk=self.lead_create()['lead_id'])
        task = lead.next_task
        params = dict(actor=self.user, organization=self.org, operation_key='reuse-selected-responsible',
            command_type='lead.qualify', subject_id=lead.pk, expected_revision=lead.revision,
            changes={'expected_task_version': version_for(task), 'deals': [{'pipeline_id': self.pipeline.pk,
                'title': 'Selected reused step', 'responsible_id': self.recipient.pk, 'reuse_next_action': True,
                'next_action': {'title': task.title, 'due_at': task.due_at.isoformat()}}]})
        result = execute_lead_command(**params)
        deal = CrmDeal.objects.get(pk=result['deal_ids'][0])
        task.refresh_from_db()
        self.assertEqual(deal.next_action.task_link.task_id, task.pk)
        self.assertEqual(task.primary_responsible_id, self.recipient.pk)
        self.assertEqual(execute_lead_command(**params), result)


class CrmAssignmentSourceTests(InboxFixture, TestCase):
    def test_actual_message_source_access_is_required_before_assignment(self):
        from pool_service.communication_models import CommunicationAccess
        from pool_service.crm_commercial_models import CrmIntakeActivation
        from pool_service.services.crm_inbox import claim_one, deliver_claim
        from pool_service.services.crm_lead_intake import consume_incoming
        self.org.crm_v2_read = self.org.crm_v2_write = self.org.crm_intake_process = True
        self.org.paid_until = self.now + timedelta(days=30)
        self.org.save()
        CrmIntakeActivation.objects.create(organization=self.org, starts_at=self.now - timedelta(minutes=30))
        actor = get_user_model().objects.create_user('source-assignment-actor')
        recipient = get_user_model().objects.create_user('source-assignment-recipient')
        for user in (actor, recipient):
            OrganizationAccess.objects.create(organization=self.org, user=user, role='manager')
            CrmAccess.objects.create(organization=self.org, user=user, view_leads=True,
                create_own_leads=True, take_leads=True, edit_own_leads=True, transition_own_leads=True)
        CommunicationAccess.objects.filter(user=actor, organization=self.org).update(can_view_conversations=True)
        CommunicationAccess.objects.filter(user=recipient, organization=self.org).update(can_view_conversations=False)
        self.incoming()
        self.assertEqual(deliver_claim(*claim_one(**self.scope()), consumer=consume_incoming), 'done')
        lead = CrmLead.objects.get()
        execute_lead_command(actor=actor, organization=self.org, operation_key='source-take', command_type='lead.take',
            subject_id=lead.pk, expected_revision=lead.revision,
            changes={'next_action': {'title': 'Source follow-up', 'due_at': (self.now + timedelta(days=1)).isoformat()}})
        lead.refresh_from_db()
        values = dict(actor=actor, organization=self.org, operation_key='source-assignment', command_type='lead.assign',
            subject_id=lead.pk, expected_revision=lead.revision, changes={'responsible_id': recipient.pk})
        with self.assertRaises(PermissionDenied): execute_assignment(**values)
        self.assertNotIn(recipient.pk, [row['id'] for row in responsible_candidates(
            actor=actor, organization=self.org, kind='lead', subject_id=lead.pk)['users']])
        self.assertFalse(Notification.objects.filter(user=recipient, kind='crm_assignment').exists())
        CommunicationAccess.objects.filter(user=recipient, organization=self.org).update(can_view_conversations=True)
        execute_assignment(**values)
        lead.refresh_from_db(); self.assertEqual(lead.assignee_id, recipient.pk)
        self.assertEqual(Notification.objects.filter(user=recipient, kind='crm_assignment').count(), 1)


class CrmAssignmentAutocommitTests(PilotFixture, TransactionTestCase):
    def test_deal_details_are_read_only_in_autocommit_and_preserve_object_scope(self):
        from pool_service.client_crm_models import ClientOwnedObject
        from pool_service.models import Client, Pool

        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=10)
        self.org.save(update_fields=['plan_type', 'paid_until'])
        self.grant.view_team_deals = True
        self.grant.save(update_fields=['view_team_deals'])
        http = HttpClient()
        http.force_login(self.user)
        pool = Pool.objects.create(organization=self.org, client=self.client_record,
            address='Synthetic autocommit pool address')
        owned = ClientOwnedObject.objects.create(client=self.client_record, object_type='hammam',
            name='Synthetic autocommit object', address='Synthetic owned address')
        deals = []
        for index, fields in enumerate(({}, {'object_kind': 'pool', 'pool_id': pool.pk},
                {'object_kind': 'client_object', 'client_object_id': owned.pk})):
            deal = CrmDeal.objects.get(pk=self.create(operation_key=f'autocommit-{index}', card_fields=fields)['deal_id'])
            deals.append(deal)
        before = (CrmCommand.objects.count(), CrmEvent.objects.count(), Notification.objects.count())
        self.assertFalse(connection.in_atomic_block)
        self.assertTrue(connection.get_autocommit())
        # SQLite ignores SELECT FOR UPDATE. Exercise Django's real autocommit
        # guard locally as well as running this TransactionTestCase on MySQL.
        with patch.object(connection.features, 'has_select_for_update', True):
            for deal in deals:
                response = http.get(reverse('crm_live_detail', args=[deal.pk]), {'org_id': self.org.pk})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()['deal']['id'], deal.pk)
                self.assertTrue(response.json()['deal']['can_assign'])
        other = Client.objects.create(organization=self.org, name='Synthetic reparent target')
        pool.client = other
        pool.save(update_fields=['client'])
        with patch.object(connection.features, 'has_select_for_update', True):
            response = http.get(reverse('crm_live_detail', args=[deals[1].pk]), {'org_id': self.org.pk})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['deal']['object'], {'restricted': True})
            self.assertFalse(response.json()['deal']['can_assign'])
            self.assertFalse(response.json()['deal']['can_edit'])
            self.assertNotContains(response, 'Synthetic autocommit pool address')
        self.assertEqual(before, (CrmCommand.objects.count(), CrmEvent.objects.count(), Notification.objects.count()))
