"""Synthetic cross-surface security, historical identity and offboarding checks."""
from datetime import timedelta
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, Client as HttpClient
from django.urls import reverse
from django.utils import timezone
from pool_service.tests.test_crm_lead_commands import LeadFixture
from pool_service.models import Organization, OrganizationAccess, Employee, DataAuditLog, ClientAccess, Client, ServiceTask
from pool_service.communication_models import PhoneCall, TelephonyConnection, CommunicationAccess
from pool_service.crm_commercial_models import CrmAccess, CrmDeal, CrmEvent
from pool_service.staff_call_models import EmployeeTelephoneAssignment, CallPrivacyHold, CallWorkContext, StaffAccount
from pool_service.services.call_access import scope_calls, can_read_call, set_work_context
from pool_service.services.call_processing_settings import add_private_numbers, remove_private_number
from pool_service.call_processing_models import CallPrivateNumber
from pool_service.services.call_privacy import is_private_call
from pool_service.services.staff_lifecycle import dismiss_employee, assign_phone, close_phone


class StaffCallControlTests(LeadFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.org.plan_type = Organization.PLAN_COMPANY_PAID
        self.org.paid_until = timezone.now() + timedelta(days=30)
        self.org.save()
        self.owner = get_user_model().objects.create_user('synthetic-call-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        CrmAccess.objects.create(organization=self.org, user=self.owner, view_leads=True,
            view_team_deals=True, edit_own_leads=True, edit_own_deals=True)
        self.other = get_user_model().objects.create_user('synthetic-call-colleague')
        OrganizationAccess.objects.create(organization=self.org, user=self.other, role='manager')
        CrmAccess.objects.create(user=self.other, organization=self.org, view_leads=True)
        for user in [self.owner, self.user, self.other]:
            CommunicationAccess.objects.filter(user=user, organization=self.org).update(
                can_view_all_calls=True, can_view_own_calls=True, can_listen_calls=True)
        self.telephony = TelephonyConnection.objects.create(organization=self.org, external_id='synthetic-policy')
        self.http = HttpClient()
        self.http.force_login(self.user)

    def call(self, employee=None, **values):
        data = dict(organization=self.org, connection=self.telephony, employee=employee or self.owner,
            external_id='policy-' + str(PhoneCall.objects.count()), direction='in', phone_number='+79990001122',
            started_at=timezone.now(), duration_seconds=12, result='answered')
        data.update(values)
        return PhoneCall.objects.create(**data)

    def test_owner_call_needs_context_even_for_all_calls_permission(self):
        call = self.call()
        self.assertFalse(can_read_call(self.user, call))
        self.assertTrue(can_read_call(self.owner, call))
        self.assertEqual(scope_calls(self.user, self.org).count(), 0)

    def test_owner_previews_effective_listeners_after_assignment(self):
        call, lead = self.call(), self.create()
        url = reverse("communication_call_context", args=[call.pk])
        self.assertEqual(self.http.get(url, {"access": "1"}).status_code, 403)
        self.http.force_login(self.owner)
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=lead.pk)
        response = self.http.get(url, {"access": "1"})
        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(response.context["listeners"], [self.owner.username, self.user.username])
        CommunicationAccess.objects.filter(user=self.user, organization=self.org).update(can_listen_calls=False)
        response = self.http.get(url, {"access": "1"})
        self.assertEqual(response.context["listeners"], [self.owner.username])
        self.assertIn("no-store", response["Cache-Control"])

    def test_primary_context_assignment_changes_without_stale_user_cache(self):
        call, lead = self.call(), self.create()
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=lead.pk)
        self.assertTrue(can_read_call(self.user, call, media=True))
        self.assertFalse(can_read_call(self.other, call))
        lead.assignee = self.other
        lead.save()
        self.assertFalse(can_read_call(self.user, call))
        self.assertTrue(can_read_call(self.other, call))

    def test_owner_source_next_action_transfers_atomically_with_responsibility(self):
        from pool_service.services.crm_assignments import execute_assignment
        from pool_service.services.task_feedback import version_for
        grant = CrmAccess.objects.get(user=self.other, organization=self.org)
        grant.edit_own_leads = True
        grant.save(update_fields=['edit_own_leads'])
        call, lead = self.call(), self.create()
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=lead.pk)
        task = lead.next_task
        task.payload_json = {**task.payload_json, 'source_call_id': call.pk}
        task.save(update_fields=['payload_json'])
        execute_assignment(actor=self.user, organization=self.org, operation_key='owner-next-step-transfer',
            command_type='lead.assign', subject_id=lead.pk, expected_revision=lead.revision,
            changes={'responsible_id': self.other.pk, 'transfer_next_action': True,
                'expected_task_version': version_for(task)})
        task.refresh_from_db()
        self.assertEqual(task.primary_responsible_id, self.other.pk)
        self.assertFalse(can_read_call(self.user, call, media=True))
        self.assertTrue(can_read_call(self.other, call, media=True))

    def test_phone_metadata_backfill_preserves_sources_and_closes_stale_index(self):
        from importlib import import_module
        from types import SimpleNamespace
        from django.apps import apps
        from django.db import connection
        from pool_service.services.crm_lead_access import _without_private_calls
        call = self.call(phone_number='8 (999) 000-11-22')
        PhoneCall.objects.filter(pk=call.pk).update(privacy_phone_key='', privacy_phone_source='', owner_user_snapshot=None)
        migration = import_module('pool_service.migrations.0192_call_privacy_phone_index')
        migration.populate_metadata(apps, SimpleNamespace(connection=connection))
        call.refresh_from_db()
        self.assertEqual(call.privacy_phone_key, '9990001122')
        self.assertEqual(call.owner_user_snapshot, self.owner.pk)
        self.assertEqual(call.phone_number, '8 (999) 000-11-22')
        PhoneCall.objects.filter(pk=call.pk).update(phone_number='+7 999 333-22-11')
        self.assertFalse(_without_private_calls(PhoneCall.objects.filter(pk=call.pk)).exists())

    def test_context_from_foreign_organization_is_rejected(self):
        call = self.call()
        org = Organization.objects.create(name='Synthetic foreign')
        from pool_service.crm_commercial_models import CrmLead
        lead = CrmLead.objects.create(organization=org, title='Synthetic foreign card')
        with self.assertRaises(PermissionDenied):
            set_work_context(actor=self.owner, call_id=call.pk, lead_id=lead.pk)
        self.assertFalse(CallWorkContext.objects.exists())

    def test_employee_cannot_delegate_owner_conversation(self):
        call, lead = self.call(), self.create()
        with self.assertRaises(PermissionDenied):
            set_work_context(actor=self.user, call_id=call.pk, lead_id=lead.pk)

    def test_explicit_lead_keeps_its_assignee_after_conversion_to_another_manager(self):
        from pool_service.crm_commercial_models import CrmLeadDealLink, CrmCommand, CrmStage
        call, lead = self.call(), self.create()
        deal = CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline,
            stage=CrmStage.objects.filter(pipeline=self.pipeline).first(),
            title='Synthetic separate manager', manager=self.other)
        command = CrmCommand.objects.create(organization=self.org, actor=self.user,
            operation_key='synthetic-main-context-conversion', command_type='lead.qualify', payload_hash='c'*64)
        CrmLeadDealLink.objects.create(organization=self.org, lead=lead, deal=deal,
            command=command, kind='qualification')
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=lead.pk)
        self.assertTrue(can_read_call(self.user, call, media=True))
        self.assertFalse(can_read_call(self.other, call, media=True))
        set_work_context(actor=self.owner, call_id=call.pk, deal_id=deal.pk, revision=1)
        self.assertFalse(can_read_call(self.user, call, media=True))
        self.assertTrue(can_read_call(self.other, call, media=True))

    def test_owner_cannot_select_another_owners_private_card_or_unhide_it_by_post(self):
        from pool_service.services.crm_lead_access import can_read_lead
        lead = self.create(title='Synthetic other-owner private card sentinel')
        private = self.call()
        set_work_context(actor=self.owner, call_id=private.pk, lead_id=lead.pk)
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic former owner private', numbers_text=private.phone_number)
        OrganizationAccess.objects.filter(organization=self.org, user=self.owner, role='owner').delete()
        OrganizationAccess.objects.create(organization=self.org, user=self.other, role='owner')
        work = self.call(employee=self.other, phone_number='+79991110099')
        self.http.force_login(self.other)
        url = reverse('communication_call_context', args=[work.pk])
        response = self.http.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Synthetic other-owner private card sentinel')
        self.assertEqual(self.http.post(url, {'subject': f'lead:{lead.pk}', 'revision': 0}).status_code, 403)
        self.assertFalse(CallWorkContext.objects.filter(call=work).exists())
        self.assertFalse(can_read_lead(self.other, self.org, lead))

    def test_private_card_tasks_without_call_id_and_children_are_protected(self):
        from pool_service.services.task_read_permissions import readable_tasks
        from pool_service.services.call_privacy import task_source_is_private
        from pool_service.services.call_access import can_receive_task_notice
        call = self.call()
        private = self.create(next_action=self.action('Synthetic private next action sentinel'))
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=private.pk)
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic private task', numbers_text=call.phone_number)
        child = ServiceTask.objects.create(organization=self.org, created_by=self.user,
            parent_task=private.next_task, title='Synthetic private child', start_date=timezone.localdate())
        working = self.create(next_action=self.action('Synthetic independent working task'))
        self.grant.view_leads = False
        self.grant.save(update_fields=['view_leads'])
        tasks = readable_tasks(ServiceTask.objects.filter(pk__in=[private.next_task_id, child.pk, working.next_task_id]), self.user)
        self.assertEqual([task.pk for task in tasks], [working.next_task_id])
        self.assertTrue(task_source_is_private(private.next_task))
        self.assertFalse(can_receive_task_notice(self.user, reverse('task_edit', args=[private.next_task_id])))
        self.assertFalse(self.http.get(reverse('task_edit', args=[private.next_task_id])).status_code == 200)
        mark = CallPrivateNumber.objects.get(owner=self.owner)
        remove_private_number(user=self.owner, organization=self.org, number_id=mark.pk)
        self.assertTrue(task_source_is_private(child, user=self.user))

    def test_bulk_stage_does_not_modify_or_disclose_owner_private_card(self):
        call, private, working = self.call(), self.create(), self.create()
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=private.pk)
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic bulk private scope', numbers_text=call.phone_number)
        commands = [dict(operation_key='synthetic-batch-private-' + str(index), command_type='lead.disposition',
            subject_id=lead.pk, expected_revision=lead.revision,
            changes={'column': 'unparsed', 'reason': 'Synthetic batch review'}) for index, lead in enumerate([private, working])]
        response = self.http.post(reverse('crm_live_lead_bulk_command') + '?org_id=' + str(self.org.pk),
            {'commands': commands}, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['status'] for row in response.json()['results']], [403, 200])
        self.assertNotIn('result', response.json()['results'][0])
        revision = private.revision
        private.refresh_from_db()
        self.assertEqual(private.revision, revision)

    def test_bulk_and_single_stage_reject_private_deal_before_write_or_replay(self):
        from pool_service.crm_commercial_models import CrmCommand
        from pool_service.services.crm_commercial_access import deals_for_user
        self.grant.transition_own_deals = True
        self.grant.save(update_fields=['transition_own_deals'])
        stage = self.pipeline.stages.get(code='technical')
        deals = [CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline,
            stage=self.pipeline.stages.order_by('position').first(), manager=self.user,
            title='Synthetic stage privacy ' + str(index)) for index in range(2)]
        call = self.call()
        set_work_context(actor=self.owner, call_id=call.pk, deal_id=deals[0].pk)
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic private deal', numbers_text=call.phone_number)
        self.assertFalse(deals_for_user(self.user, self.org).filter(pk=deals[0].pk).exists())
        commands = [dict(operation_key='synthetic-private-deal-stage-' + str(index), command_type='deal.transition',
            subject_id=deal.pk, expected_revision=deal.revision, changes={'stage_id': stage.pk, 'reason': ''})
            for index, deal in enumerate(deals)]
        before = (deals[0].stage_id, deals[0].revision)
        url = reverse('crm_live_bulk_command') + '?org_id=' + str(self.org.pk)
        response = self.http.post(url, {'commands': commands}, content_type='application/json')
        self.assertEqual([row['status'] for row in response.json()['results']], [403, 200])
        deals[0].refresh_from_db()
        self.assertEqual((deals[0].stage_id, deals[0].revision), before)
        self.assertFalse(CrmCommand.objects.filter(operation_key=commands[0]['operation_key']).exists())
        self.assertFalse(CrmEvent.objects.filter(deal=deals[0]).exists())
        response = self.http.post(reverse('crm_live_command') + '?org_id=' + str(self.org.pk),
            commands[0], content_type='application/json')
        self.assertEqual(response.status_code, 403)
        second_call = self.call(phone_number='+79990003344')
        set_work_context(actor=self.owner, call_id=second_call.pk, deal_id=deals[1].pk)
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic changed privacy', numbers_text=second_call.phone_number)
        count = CrmEvent.objects.count()
        response = self.http.post(url, {'commands': [commands[1]]}, content_type='application/json')
        self.assertEqual(response.json()['results'][0]['status'], 403)
        self.assertEqual(CrmEvent.objects.count(), count)

    def test_new_requests_to_audio_and_transcript_refuse_unassigned_owner_call(self):
        call = self.call()
        for name in ['communication_call_recording', 'communication_call_analysis_transcript', 'communication_call_analysis_status']:
            response = self.http.get(reverse(name, args=[call.pk]), {'org_id': self.org.pk})
            self.assertEqual(response.status_code, 403)

    def test_technical_administrator_and_service_account_have_no_content(self):
        call, lead = self.call(), self.create()
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=lead.pk)
        StaffAccount.objects.create(organization=self.org, user=self.user, kind='service')
        self.assertFalse(can_read_call(self.user, call))

    def test_technical_admin_has_no_raw_django_admin_bypass(self):
        call = self.call(contact_name='Synthetic protected contact', recording_ref='synthetic-protected-audio')
        root = get_user_model().objects.create_superuser('synthetic-technical-root', password='synthetic-test-only')
        self.http.force_login(root)
        for path in [reverse('admin:pool_service_phonecall_changelist'),
                reverse('admin:pool_service_phonecall_change', args=[call.pk]),
                reverse('admin:pool_service_phonecall_delete', args=[call.pk])]:
            response = self.http.get(path)
            self.assertEqual(response.status_code, 403)
            self.assertNotContains(response, 'synthetic-protected-audio', status_code=403)
        response = self.http.post(reverse('admin:pool_service_phonecall_delete', args=[call.pk]), {'post': 'yes'})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(PhoneCall.objects.filter(pk=call.pk).exists())
        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        self.assertFalse(can_read_call(self.user, call))

    def test_personal_rule_is_owner_scoped_and_removal_does_not_reopen_history(self):
        old = self.call()
        work = self.call(employee=self.user)
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic private', numbers_text=old.phone_number)
        self.assertTrue(is_private_call(old))
        self.assertFalse(is_private_call(work))
        mark = CallPrivateNumber.objects.get(owner=self.owner)
        remove_private_number(user=self.owner, organization=self.org, number_id=mark.pk)
        self.assertTrue(is_private_call(old))
        self.assertTrue(CallPrivacyHold.objects.filter(call=old, released_at__isnull=True).exists())
        new = self.call()
        self.assertFalse(is_private_call(new))
        self.assertFalse(is_private_call(work))

    def test_private_only_manual_context_and_converted_card_hide_but_work_history_survives(self):
        from pool_service.crm_commercial_models import CrmLeadDealLink, CrmStage, CrmCommand
        from pool_service.services.crm_lead_access import can_read_lead
        from pool_service.services.crm_commercial_access import deals_for_user
        contact = Client.objects.create(organization=self.org, name='Synthetic combined contact', client_type='private')
        lead = self.create(client_id=contact.pk)
        stage = CrmStage.objects.filter(pipeline=self.pipeline).order_by('pk').first()
        deal = CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline, stage=stage,
            title='Synthetic combined deal', manager=self.user, client=contact)
        command = CrmCommand.objects.create(organization=self.org, actor=self.user,
            operation_key='synthetic-private-conversion', command_type='lead.qualify', payload_hash='f'*64)
        CrmLeadDealLink.objects.create(organization=self.org, lead=lead, deal=deal,
            command=command, kind='qualification')
        private = self.call(client=contact)
        set_work_context(actor=self.owner, call_id=private.pk, lead_id=lead.pk)
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic personal', numbers_text=private.phone_number)
        self.assertFalse(can_read_lead(self.user, self.org, lead))
        self.assertFalse(deals_for_user(self.user, self.org).filter(pk=deal.pk).exists())
        self.assertEqual(self.http.get(reverse('crm_live_detail', args=[deal.pk]), {'org_id': self.org.pk}).status_code, 404)
        work = self.call(employee=self.user, client=contact)
        self.assertTrue(can_read_lead(self.user, self.org, lead))
        self.assertTrue(deals_for_user(self.user, self.org).filter(pk=deal.pk).exists())
        self.assertTrue(can_read_call(self.user, work))
        self.assertFalse(can_read_call(self.user, private))
        self.assertTrue(CallPrivacyHold.objects.filter(call=private, released_at__isnull=True).exists())

    def test_unknown_legacy_number_is_not_confirmed_private_evidence(self):
        from pool_service.services.crm_card_privacy import exclude_private_only_cards
        from pool_service.crm_commercial_models import CrmLead
        call, lead = self.call(phone_number='legacy-unknown'), self.create()
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=lead.pk)
        self.assertTrue(exclude_private_only_cards(CrmLead.objects.filter(pk=lead.pk), self.org, kind='lead').exists())

    def test_removal_boundary_protects_late_history_and_owner_can_restore_one_saved_call(self):
        from pool_service.services.crm_lead_access import _without_private_calls
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic archived personal number',
            numbers_text='+79990001122')
        rule = CallPrivateNumber.objects.get(owner=self.owner)
        remove_private_number(user=self.owner, organization=self.org, number_id=rule.pk)
        rule.refresh_from_db()
        old = self.call(started_at=rule.removed_at-timedelta(days=1))
        new = self.call(started_at=rule.removed_at+timedelta(seconds=2))
        employee_work = self.call(employee=self.user, started_at=old.started_at)
        self.assertTrue(is_private_call(old))
        self.assertFalse(is_private_call(new))
        self.assertFalse(is_private_call(employee_work))
        self.assertTrue(CallPrivacyHold.objects.filter(call=old, owner=self.owner).exists())
        # Trusted legacy fixture: the SQL archive must protect a row even if
        # it predates automatic per-call hold creation.
        CallPrivacyHold.objects.filter(call=old).delete()
        self.assertFalse(_without_private_calls(PhoneCall.objects.filter(pk=old.pk)).exists())
        self.assertTrue(_without_private_calls(PhoneCall.objects.filter(pk=new.pk)).exists())
        old.save(update_fields=['duration_seconds'])
        hold = CallPrivacyHold.objects.get(call=old, owner=self.owner)
        self.http.force_login(self.owner)
        response = self.http.post(reverse('communication_personal_history')+'?org_id='+str(self.org.pk), {'hold': hold.pk})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(is_private_call(old))
        self.assertTrue(_without_private_calls(PhoneCall.objects.filter(pk=old.pk)).exists())
        old.save(update_fields=['duration_seconds'])
        self.assertFalse(is_private_call(old))
        lead = self.create()
        set_work_context(actor=self.owner, call_id=old.pk, lead_id=lead.pk)
        self.assertTrue(can_read_call(self.user, old, media=True))

    def test_personal_archive_preserves_microsecond_boundary_in_python_and_sql(self):
        from pool_service.services.crm_lead_access import _without_private_calls
        boundary = timezone.now().replace(microsecond=123456)
        CallPrivateNumber.objects.create(organization=self.org, owner=self.owner, phone_key='9990001122',
            label='Synthetic precise removal boundary', removed_at=boundary)
        for has_provider_time in [False, True]:
            for offset in [-1, 0, 1]:
                with self.subTest(provider_time=has_provider_time, microseconds=offset):
                    occurred_at = boundary + timedelta(microseconds=offset)
                    values = {'started_at': occurred_at}
                    if has_provider_time:
                        values.update(started_at=boundary+timedelta(days=1),
                            intake_scope={'organization': self.org.pk}, intake_occurred_at=occurred_at)
                    call = self.call(**values)
                    CallPrivacyHold.objects.filter(call=call).delete()
                    self.assertEqual(is_private_call(call), offset < 0)
                    self.assertEqual(_without_private_calls(PhoneCall.objects.filter(pk=call.pk)).exists(), offset >= 0)

    def test_removed_rule_fails_closed_for_unknown_provider_time(self):
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic unknown-time archive',
            numbers_text='+79990001122')
        rule = CallPrivateNumber.objects.get(owner=self.owner)
        remove_private_number(user=self.owner, organization=self.org, number_id=rule.pk)
        rule.refresh_from_db()
        call = self.call(started_at=rule.removed_at+timedelta(seconds=2), intake_scope={'organization': self.org.pk},
            intake_occurred_at=None)
        self.assertTrue(is_private_call(call))
        self.assertTrue(CallPrivacyHold.objects.filter(call=call, owner=self.owner).exists())

    def test_background_partial_save_does_not_undo_an_explicit_owner_restore(self):
        from unittest.mock import patch
        call = self.call()
        hold = CallPrivacyHold.objects.create(call=call, organization=self.org, owner=self.owner)
        self.http.force_login(self.owner)
        # Simulate a privacy decision read immediately before the owner restores.
        with patch('pool_service.services.call_privacy.private_call_owner_pairs', return_value={(call.pk, self.owner.pk)}):
            response = self.http.post(reverse('communication_personal_history')+'?org_id='+str(self.org.pk), {'hold': hold.pk})
            self.assertEqual(response.status_code, 302)
            call.save(update_fields=['duration_seconds'])
        hold.refresh_from_db()
        self.assertIsNotNone(hold.released_at)
        self.assertFalse(is_private_call(call))

    def test_releasing_one_owners_archive_does_not_release_another_owners_archive(self):
        from pool_service.services.crm_lead_access import _without_private_calls
        boundary = timezone.now()
        for owner in [self.owner, self.other]:
            CallPrivateNumber.objects.create(organization=self.org, owner=owner, phone_key='9990001122',
                label='Synthetic independent owner archive', removed_at=boundary)
        call = self.call(peer_employee=self.other, started_at=boundary-timedelta(days=1))
        CallPrivacyHold.objects.filter(call=call, owner=self.owner).update(released_at=timezone.now())
        CallPrivacyHold.objects.filter(call=call, owner=self.other).delete()
        self.assertTrue(is_private_call(call))
        self.assertFalse(_without_private_calls(PhoneCall.objects.filter(pk=call.pk)).exists())
        CallPrivacyHold.objects.create(call=call, owner=self.other, organization=self.org, released_at=timezone.now())
        self.assertFalse(is_private_call(call))
        self.assertTrue(_without_private_calls(PhoneCall.objects.filter(pk=call.pk)).exists())

    def test_private_duplicate_does_not_hide_deal_from_independent_target(self):
        from pool_service.crm_commercial_models import CrmLeadDealLink, CrmStage, CrmCommand
        from pool_service.services.crm_lead_access import can_read_lead
        from pool_service.services.crm_commercial_access import deals_for_user
        target, contributor = self.create(), self.create()
        call = self.call()
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=contributor.pk)
        contributor.status, contributor.result, contributor.duplicate_of = 'closed', 'duplicate', target
        contributor.save()
        deal = CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline,
            stage=CrmStage.objects.filter(pipeline=self.pipeline).first(), manager=self.user,
            title='Synthetic independently qualified deal')
        command = CrmCommand.objects.create(organization=self.org, actor=self.user,
            operation_key='synthetic-independent-conversion', command_type='lead.qualify', payload_hash='a'*64)
        CrmLeadDealLink.objects.create(organization=self.org, lead=target, deal=deal,
            command=command, kind='qualification')
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic private contributor', numbers_text=call.phone_number)
        self.assertTrue(can_read_lead(self.user, self.org, target))
        self.assertTrue(deals_for_user(self.user, self.org).filter(pk=deal.pk).exists())
        self.assertFalse(can_read_lead(self.user, self.org, contributor))
        self.assertFalse(can_read_call(self.user, call))

    def test_private_lead_link_does_not_hide_preexisting_business_deal(self):
        from pool_service.crm_commercial_models import CrmLeadDealLink, CrmStage, CrmCommand
        from pool_service.services.crm_commercial_access import deals_for_user
        lead, call = self.create(), self.call()
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=lead.pk)
        deal = CrmDeal.objects.create(organization=self.org, pipeline=self.pipeline,
            stage=CrmStage.objects.filter(pipeline=self.pipeline).first(), manager=self.user,
            title='Synthetic preexisting deal')
        command = CrmCommand.objects.create(organization=self.org, actor=self.user,
            operation_key='synthetic-private-existing-link', command_type='lead.transition', payload_hash='b'*64)
        CrmLeadDealLink.objects.create(organization=self.org, lead=lead, deal=deal,
            command=command, kind='existing')
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic private linked lead', numbers_text=call.phone_number)
        self.assertTrue(deals_for_user(self.user, self.org).filter(pk=deal.pk).exists())
        self.assertFalse(can_read_call(self.user, call))
        self.grant.transition_own_deals = True
        self.grant.save(update_fields=['transition_own_deals'])
        response = self.http.post(reverse('crm_live_bulk_command') + '?org_id=' + str(self.org.pk),
            {'commands': [dict(operation_key='synthetic-mixed-deal-stage', command_type='deal.transition',
                subject_id=deal.pk, expected_revision=deal.revision,
                changes={'stage_id': self.pipeline.stages.get(code='technical').pk, 'reason': ''})]}, content_type='application/json')
        self.assertEqual(response.json()['results'][0]['status'], 200)

    def test_inactive_account_links_existing_hr_record_without_duplicate_or_call_rewrite(self):
        self.other.is_active = False
        self.other.save(update_fields=['is_active'])
        employee = Employee.objects.create(organization=self.org, display_name='Synthetic existing HR person')
        self.call(employee=self.other, employee_profile=employee)
        before = list(PhoneCall.objects.values())
        count = Employee.objects.count()
        self.http.force_login(self.owner)
        response = self.http.post(reverse('staff_account_kind', args=[self.org.pk, self.other.pk]),
            {'kind': 'person', 'employee': employee.pk, 'revision': 0})
        self.assertEqual(response.status_code, 302)
        employee.refresh_from_db(); self.other.refresh_from_db()
        self.assertEqual(employee.user_id, self.other.pk)
        self.assertFalse(self.other.is_active)
        self.assertEqual(Employee.objects.count(), count)
        self.assertEqual(list(PhoneCall.objects.values()), before)
        self.assertTrue(DataAuditLog.objects.filter(entity_type='Employee', entity_id=str(employee.pk),
            changed_fields=['user_id']).exists())

    def test_person_account_requires_explicit_profile_choice_and_rejects_foreign_or_occupied(self):
        self.http.force_login(self.owner)
        url = reverse('staff_account_kind', args=[self.org.pk, self.other.pk])
        foreign = Employee.objects.create(organization=Organization.objects.create(name='Synthetic foreign HR'),
            display_name='Synthetic foreign person')
        count = Employee.objects.count()
        for selected in ['', self.employee.pk, foreign.pk]:
            with self.subTest(selected=selected):
                response = self.http.post(url, {'kind': 'person', 'employee': selected, 'revision': 0})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context['account'].revision, 0)
                self.assertFalse(StaffAccount.objects.filter(organization=self.org, user=self.other).exists())
                self.assertEqual(Employee.objects.count(), count)
        self.assertEqual(self.http.post(url, {'kind': 'person', 'employee': 'new', 'revision': 0}).status_code, 302)
        employee = Employee.objects.get(organization=self.org, user=self.other)
        self.assertEqual(Employee.objects.count(), count + 1)
        self.assertEqual(self.http.post(url, {'kind': 'person', 'employee': self.employee.pk, 'revision': 1}).status_code, 200)
        employee.refresh_from_db()
        self.assertEqual(employee.user_id, self.other.pk)

    def test_active_account_cannot_be_attached_to_dismissed_profile(self):
        employee = Employee.objects.create(organization=self.org, display_name='Synthetic former person',
            is_active=False, employment_status='dismissed')
        self.http.force_login(self.owner)
        response = self.http.post(reverse('staff_account_kind', args=[self.org.pk, self.other.pk]),
            {'kind': 'person', 'employee': employee.pk, 'revision': 0})
        self.assertEqual(response.status_code, 200)
        employee.refresh_from_db()
        self.assertIsNone(employee.user_id)
        self.assertFalse(StaffAccount.objects.filter(organization=self.org, user=self.other).exists())

    def test_unlinked_employee_dismissal_is_audited_without_touching_unassigned_cards(self):
        employee = Employee.objects.create(organization=self.org, display_name='Synthetic unlinked former person')
        lead = self.create(); lead.assignee = None; lead.save()
        dismiss_employee(actor=self.owner, employee_id=employee.pk, expected_updated_at=employee.updated_at.isoformat())
        employee.refresh_from_db(); lead.refresh_from_db()
        self.assertFalse(employee.is_active)
        self.assertEqual(employee.employment_status, 'dismissed')
        self.assertIsNone(lead.assignee_id)
        event = DataAuditLog.objects.get(entity_type='Employee', entity_id=str(employee.pk))
        self.assertTrue(event.before['is_active'])
        self.assertFalse(event.after['is_active'])

    def test_lazy_card_fast_path_closes_when_a_personal_rule_appears(self):
        from pool_service.crm_commercial_models import CrmLead
        from pool_service.services.crm_card_privacy import exclude_private_only_cards
        private, public = self.create(), self.create()
        call = self.call()
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=private.pk)
        cards = CrmLead.objects.filter(pk__in=[private.pk, public.pk])
        pending = exclude_private_only_cards(cards, self.org, kind='lead')
        CallPrivateNumber.objects.create(organization=self.org, owner=self.owner, phone_key=call.privacy_phone_key)
        self.assertEqual(list(pending), [])
        self.assertEqual(set(exclude_private_only_cards(cards, self.org, kind='lead').values_list('pk', flat=True)), {public.pk})

    def test_lazy_card_fast_path_closes_when_a_privacy_hold_appears(self):
        from pool_service.crm_commercial_models import CrmLead
        from pool_service.services.crm_card_privacy import exclude_private_only_cards
        private, public = self.create(), self.create()
        call = self.call()
        set_work_context(actor=self.owner, call_id=call.pk, lead_id=private.pk)
        cards = CrmLead.objects.filter(pk__in=[private.pk, public.pk])
        pending = exclude_private_only_cards(cards, self.org, kind='lead')
        CallPrivacyHold.objects.create(call=call, organization=self.org, owner=self.owner)
        self.assertEqual(list(pending), [])
        self.assertEqual(set(exclude_private_only_cards(cards, self.org, kind='lead').values_list('pk', flat=True)), {public.pk})

    def test_only_owner_can_return_held_history(self):
        call = self.call()
        hold = CallPrivacyHold.objects.create(call=call, organization=self.org, owner=self.owner)
        response = self.http.post(reverse('communication_personal_history'), {'hold': hold.pk})
        self.assertEqual(response.status_code, 403)
        hold.refresh_from_db()
        self.assertIsNone(hold.released_at)

    def test_successive_owners_keep_independent_protection_for_shared_history(self):
        shared = self.call(peer_employee=self.other)
        add_private_numbers(user=self.owner, organization=self.org, label='Synthetic first owner', numbers_text=shared.phone_number)
        first_mark = CallPrivateNumber.objects.get(owner=self.owner)
        remove_private_number(user=self.owner, organization=self.org, number_id=first_mark.pk)
        first_hold = CallPrivacyHold.objects.get(call=shared, owner=self.owner)
        OrganizationAccess.objects.filter(organization=self.org, user=self.owner, role='owner').delete()
        OrganizationAccess.objects.create(organization=self.org, user=self.other, role='owner')
        add_private_numbers(user=self.other, organization=self.org, label='Synthetic next owner', numbers_text=shared.phone_number)
        second_mark = CallPrivateNumber.objects.get(owner=self.other)
        remove_private_number(user=self.other, organization=self.org, number_id=second_mark.pk)
        second_hold = CallPrivacyHold.objects.get(call=shared, owner=self.other)
        self.assertEqual(CallPrivacyHold.objects.filter(call=shared).count(), 2)
        self.http.force_login(self.other)
        url = reverse('communication_personal_history')
        self.assertEqual(self.http.post(url, {'hold': first_hold.pk}).status_code, 404)
        self.assertEqual(self.http.post(url, {'hold': second_hold.pk}).status_code, 302)
        self.assertTrue(is_private_call(shared))
        OrganizationAccess.objects.filter(organization=self.org, user=self.other, role='owner').delete()
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        self.http.force_login(self.owner)
        self.assertEqual(self.http.post(url, {'hold': first_hold.pk}).status_code, 302)
        self.assertFalse(is_private_call(shared))

    def test_as_of_number_classification_preserves_past_after_dismissal(self):
        now = timezone.now()
        peer = Employee.objects.create(organization=self.org, user=self.other, display_name='Synthetic peer')
        first = assign_phone(actor=self.owner, employee=self.employee, connection=self.telephony,
            extension='101', valid_from=now-timedelta(days=4), valid_to=now-timedelta(days=1))
        second = assign_phone(actor=self.owner, employee=peer, connection=self.telephony,
            phone_key='9990001122', valid_from=now-timedelta(days=4))
        old = self.call(employee=self.user, provider_extension='101', started_at=now-timedelta(days=2))
        self.assertEqual(old.direction, 'internal')
        self.assertEqual(old.classification_evidence['assignments'], [first.pk, second.pk])
        self.employee.is_active = False
        self.employee.employment_status = 'dismissed'
        self.employee.save()
        old.refresh_from_db()
        self.assertEqual(old.direction, 'internal')
        new = self.call(employee=self.user, provider_extension='101', started_at=now)
        self.assertEqual(new.direction, 'in')
        self.assertTrue(can_read_call(self.owner, old, media=True))

    def test_reused_pbx_identity_cannot_transfer_confirmed_old_participants(self):
        from pool_service.communication_models import TelephonyEmployeeIdentity
        from pool_service.services.employee_identity_sync import apply_telephony_identity_to_calls
        now = timezone.now()
        peer = Employee.objects.create(organization=self.org, user=self.other, display_name='Synthetic old peer')
        assign_phone(actor=self.owner, employee=self.employee, connection=self.telephony,
            extension='101', provider_user='synthetic-reused-provider', valid_from=now-timedelta(days=5))
        assign_phone(actor=self.owner, employee=peer, connection=self.telephony,
            phone_key='9990001122', valid_from=now-timedelta(days=5))
        old = self.call(employee=self.user, provider_extension='101', provider_user='synthetic-reused-provider',
            started_at=now-timedelta(days=3))
        proof = old.classification_evidence.copy()
        newcomer = get_user_model().objects.create_user('synthetic-next-number-holder')
        OrganizationAccess.objects.create(organization=self.org, user=newcomer, role='manager')
        CommunicationAccess.objects.filter(organization=self.org, user=newcomer).update(
            can_view_all_calls=True, can_view_own_calls=True, can_listen_calls=True)
        fresh = Employee.objects.create(organization=self.org, user=newcomer, display_name='Synthetic next holder')
        identity = TelephonyEmployeeIdentity.objects.create(organization=self.org, connection=self.telephony,
            extension='101', external_user='synthetic-reused-provider', employee=fresh,
            status='manually_matched', confirmed_by=self.owner, confirmed_at=now)
        apply_telephony_identity_to_calls(identity, previous_employee_id=self.employee.pk, reassignment_boundary=now)
        old.refresh_from_db()
        self.assertEqual((old.employee_id, old.peer_employee_id), (self.user.pk, self.other.pk))
        self.assertEqual(old.classification_evidence, proof)
        self.assertFalse(can_read_call(newcomer, old))
        self.assertTrue(can_read_call(self.user, old))
        old.employee, old.employee_profile = newcomer, fresh
        old.peer_employee, old.peer_employee_profile = newcomer, fresh
        old.save(update_fields=['employee', 'employee_profile', 'peer_employee', 'peer_employee_profile'])
        old.refresh_from_db()
        self.assertEqual((old.employee_id, old.peer_employee_id), (self.user.pk, self.other.pk))
        self.assertFalse(can_read_call(newcomer, old, media=True))
        self.assertTrue(can_read_call(self.user, old, media=True))

    def test_overlap_shared_and_cross_connection_do_not_guess_internal(self):
        now = timezone.now()
        assign_phone(actor=self.owner, employee=self.employee, connection=self.telephony, extension='101', valid_from=now)
        with self.assertRaises(ValidationError):
            assign_phone(actor=self.owner, employee=self.employee, connection=self.telephony, extension='101', valid_from=now)
        EmployeeTelephoneAssignment.objects.create(organization=self.org, connection=self.telephony, shared=True,
            phone_key='9990001122', valid_from=now, confirmed_by=self.owner)
        call = self.call(employee=self.user, provider_extension='101', started_at=now)
        self.assertEqual(call.direction, 'in')

    def test_current_identity_cannot_transfer_legacy_internal_participants_or_unknown_side(self):
        from pool_service.communication_models import TelephonyEmployeeIdentity
        from pool_service.services.employee_identity_sync import apply_telephony_identity_to_calls
        now = timezone.now()
        peer = Employee.objects.create(organization=self.org, user=self.other, display_name='Synthetic legacy peer')
        newcomer = get_user_model().objects.create_user('synthetic-recycled-legacy-number')
        OrganizationAccess.objects.create(organization=self.org, user=newcomer, role='manager')
        fresh = Employee.objects.create(organization=self.org, user=newcomer, display_name='Synthetic new legacy holder')
        identity = TelephonyEmployeeIdentity.objects.create(organization=self.org, connection=self.telephony,
            extension='101', external_user='synthetic-recycled-provider', employee=fresh,
            status='manually_matched', confirmed_by=self.owner, confirmed_at=now)
        first = self.call(employee=self.user, employee_profile=self.employee, peer_employee=self.other,
            peer_employee_profile=peer, direction='internal', provider_extension='101',
            provider_user=identity.external_user, started_at=now-timedelta(days=10))
        second = self.call(employee=self.other, employee_profile=peer, peer_employee=self.user,
            peer_employee_profile=self.employee, direction='internal', peer_provider_extension='101',
            peer_provider_user=identity.external_user, started_at=now-timedelta(days=10))
        unknown = self.call(employee=self.other, employee_profile=peer, direction='internal',
            peer_provider_extension='101', started_at=now-timedelta(days=10))
        # Reconstruct stored pre-feature rows; current ingestion would mark
        # unsupported internal identity evidence for owner review.
        PhoneCall.objects.filter(pk__in=[first.pk, second.pk, unknown.pk]).update(classification_evidence={})
        for call in [first, second]:
            call.refresh_from_db()
            self.assertTrue(can_read_call(self.user, call, media=True))
            self.assertFalse(can_read_call(newcomer, call, media=True))
        self.assertEqual(apply_telephony_identity_to_calls(identity,
            previous_employee_id=self.employee.pk, reassignment_boundary=now-timedelta(days=1)), 0)
        for call in [first, second]:
            call.employee, call.employee_profile = newcomer, fresh
            call.peer_employee, call.peer_employee_profile = newcomer, fresh
            call.direction = 'out'
            call.save(update_fields=['employee', 'employee_profile', 'peer_employee', 'peer_employee_profile', 'direction'])
            call.refresh_from_db()
            self.assertEqual(call.direction, 'internal')
            self.assertEqual(call.classification_evidence, {})
            self.assertTrue(can_read_call(self.user, call, media=True))
            self.assertFalse(can_read_call(newcomer, call, media=True))
        unknown.refresh_from_db()
        self.assertIsNone(unknown.peer_employee_id)
        self.assertFalse(can_read_call(newcomer, unknown))
        first.provider_user = 'synthetic-different-party'
        first.save(update_fields=['provider_user'])
        first.refresh_from_db()
        self.assertEqual(first.classification_evidence['kind'], 'review')
        self.assertFalse(can_read_call(self.user, first))

    def test_late_import_and_recycled_provider_do_not_override_the_call_date(self):
        from pool_service.communication_models import TelephonyEmployeeIdentity
        from pool_service.services.employee_identity_sync import apply_telephony_identity_to_calls
        now = timezone.now()
        fresh = Employee.objects.create(organization=self.org, user=self.other, display_name='Synthetic next external holder')
        identity = TelephonyEmployeeIdentity.objects.create(organization=self.org, connection=self.telephony,
            extension='101', external_user='synthetic-late-provider', employee=fresh,
            status='manually_matched', confirmed_by=self.owner, confirmed_at=now)
        call = self.call(employee=self.user, employee_profile=self.employee, provider_extension='101',
            provider_user=identity.external_user, started_at=now-timedelta(days=10))
        self.assertEqual(apply_telephony_identity_to_calls(identity,
            previous_employee_id=self.employee.pk, reassignment_boundary=now-timedelta(days=1)), 0)
        call.refresh_from_db()
        self.assertEqual(call.employee_id, self.user.pk)

    def test_dismissal_revokes_grants_and_preserves_history_and_other_organization(self):
        org = Organization.objects.create(name='Synthetic second employer')
        OrganizationAccess.objects.create(user=self.user, organization=org, role='manager')
        lead, call = self.create(), self.call(employee=self.user)
        employee = dismiss_employee(actor=self.owner, employee_id=self.employee.pk,
            expected_updated_at=self.employee.updated_at.isoformat(), dismissed_at=timezone.localdate())
        self.assertEqual(employee.employment_status, 'dismissed')
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertFalse(OrganizationAccess.objects.filter(user=self.user, organization=self.org).exists())
        self.assertTrue(OrganizationAccess.objects.filter(user=self.user, organization=org).exists())
        call.refresh_from_db(); lead.refresh_from_db()
        self.assertEqual(call.employee_id, self.user.pk)
        self.assertIsNone(lead.assignee_id)
        self.assertTrue(CrmEvent.objects.filter(lead=lead, event_type='staff.reassigned').exists())
        self.assertTrue(DataAuditLog.objects.filter(entity_type='Employee', entity_id=str(employee.pk)).exists())
        self.assertFalse(can_read_call(self.user, call))

    def test_dismissal_last_organization_disables_existing_login(self):
        dismiss_employee(actor=self.owner, employee_id=self.employee.pk,
            expected_updated_at=self.employee.updated_at.isoformat(), dismissed_at=timezone.localdate())
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        response = self.http.get(reverse('users'))
        self.assertEqual(response.status_code, 302)

    def test_dismissal_with_unknown_date_revokes_now_without_inventing_history(self):
        employee = dismiss_employee(actor=self.owner, employee_id=self.employee.pk,
            expected_updated_at=self.employee.updated_at.isoformat())
        self.assertIsNone(employee.dismissed_at)
        self.assertFalse(employee.is_active)

    def test_bulk_owner_identity_mapping_retains_snapshot(self):
        from pool_service.communication_models import TelephonyEmployeeIdentity
        from pool_service.services.employee_identity_sync import apply_telephony_identity_to_calls
        owner_employee = Employee.objects.create(organization=self.org, user=self.owner, display_name='Synthetic owner')
        call = PhoneCall.objects.create(organization=self.org, connection=self.telephony,
            employee=None, provider_extension='201', external_id='late-bulk-owner',
            direction='in', phone_number='+79998887755', started_at=timezone.now())
        identity = TelephonyEmployeeIdentity.objects.create(organization=self.org, connection=self.telephony,
            extension='201', employee=owner_employee, status='manually_matched',
            confirmed_by=self.owner, confirmed_at=timezone.now())
        apply_telephony_identity_to_calls(identity)
        call.refresh_from_db()
        self.assertEqual(call.owner_user_snapshot, self.owner.pk)
        self.assertFalse(can_read_call(self.user, call))

    def test_all_employee_login_backfills_retain_owner_snapshot_after_role_change(self):
        from pool_service.services.employee_identity_sync import backfill_employee_calls
        now = timezone.now()
        employee = Employee.objects.create(organization=self.org, display_name='Synthetic late owner login')
        assign_phone(actor=self.owner, employee=employee, connection=self.telephony,
            extension='901', valid_from=now-timedelta(days=1))
        primary = PhoneCall.objects.create(organization=self.org, connection=self.telephony,
            provider_extension='901', external_id='late-login-primary', direction='in',
            phone_number='+79998880011', started_at=now)
        peer = self.call(employee=self.user, peer_employee_profile=employee,
            phone_number='+79998880022')
        legacy = self.call(employee=self.other, phone_number='+79998880033')
        employee.user = self.owner
        employee.save(update_fields=['user'])
        backfill_employee_calls(employee, previous_user_id=self.other.pk)
        for call in [primary, peer, legacy]:
            call.refresh_from_db()
            self.assertEqual(call.owner_user_snapshot, self.owner.pk)
        OrganizationAccess.objects.filter(user=self.owner, organization=self.org, role='owner').delete()
        OrganizationAccess.objects.create(user=self.other, organization=self.org, role='owner')
        for call in [primary, peer, legacy]:
            self.assertFalse(can_read_call(self.user, call))

    def test_directory_does_not_convert_every_account_to_employee(self):
        self.http.force_login(self.owner)
        response = self.http.get(reverse('users'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Не проверена')
        self.assertFalse(Employee.objects.filter(user=self.owner).exists())
        self.assertContains(response, self.employee.display_name)

    def test_legacy_global_block_redirects_to_scoped_preview(self):
        self.http.force_login(self.owner)
        access = OrganizationAccess.objects.get(user=self.user, organization=self.org)
        response = self.http.post(reverse('staff_toggle_block', args=[access.pk]))
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertRedirects(response, reverse('staff_employee_control', args=[self.employee.pk]))

    def test_service_account_cannot_receive_cards_or_offboarding_assignments(self):
        from pool_service.services.crm_assignments import execute_assignment, responsible_candidates
        from pool_service.services.staff_lifecycle import dismissal_recipients
        service = get_user_model().objects.create_user('synthetic-service-recipient')
        OrganizationAccess.objects.create(organization=self.org, user=service, role='manager')
        StaffAccount.objects.create(organization=self.org, user=service, kind='service')
        CrmAccess.objects.create(organization=self.org, user=service, view_leads=True, edit_own_leads=True)
        lead = self.create()
        candidates = responsible_candidates(actor=self.user, organization=self.org, kind='lead', subject_id=lead.pk)
        self.assertNotIn(service.pk, [row['id'] for row in candidates['users']])
        self.assertFalse(dismissal_recipients(self.org).filter(pk__in=[service.pk, self.other.pk]).exists())
        with self.assertRaises(PermissionDenied):
            execute_assignment(actor=self.user, organization=self.org, operation_key='reject-service-recipient',
                command_type='lead.assign', subject_id=lead.pk, expected_revision=lead.revision,
                changes={'responsible_id': service.pk})
        with self.assertRaises(ValidationError):
            dismiss_employee(actor=self.owner, employee_id=self.employee.pk,
                expected_updated_at=self.employee.updated_at.isoformat(), recipient_id=service.pk)
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.is_active)
        lead.refresh_from_db()
        self.assertEqual(lead.assignee_id, self.user.pk)

    def test_dismissal_keeps_a_separate_client_cabinet_and_rejects_stale_phone_assignment(self):
        foreign = Organization.objects.create(name='Synthetic client organization')
        client = Client.objects.create(organization=foreign, name='Synthetic customer')
        ClientAccess.objects.create(client=client, user=self.user)
        stale = Employee.objects.get(pk=self.employee.pk)
        dismiss_employee(actor=self.owner, employee_id=self.employee.pk,
            expected_updated_at=stale.updated_at.isoformat(), dismissed_at=timezone.localdate())
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertTrue(ClientAccess.objects.filter(client=client, user=self.user).exists())
        with self.assertRaises(ValidationError):
            assign_phone(actor=self.owner, employee=stale, connection=self.telephony,
                extension='999', valid_from=timezone.now())

    def test_closed_assignment_cannot_be_extended_into_next_holder(self):
        now = timezone.now()
        assignment = assign_phone(actor=self.owner, employee=self.employee, connection=self.telephony,
            extension='101', valid_from=now-timedelta(days=2))
        close_phone(actor=self.owner, assignment_id=assignment.pk, valid_to=now-timedelta(days=1))
        assign_phone(actor=self.owner, employee=self.employee, connection=self.telephony,
            extension='101', valid_from=now)
        with self.assertRaises(ValidationError):
            close_phone(actor=self.owner, assignment_id=assignment.pk, valid_to=now+timedelta(days=1))

    def test_late_owner_mapping_preserves_authority_after_owner_role_changes(self):
        call = PhoneCall.objects.create(organization=self.org, connection=self.telephony, employee=None,
            external_id='unmapped-owner', direction='in', phone_number='+79998887766', started_at=timezone.now())
        call.employee = self.owner
        call.save(update_fields=['employee'])
        call.refresh_from_db()
        self.assertEqual(call.owner_user_snapshot, self.owner.pk)
        OrganizationAccess.objects.filter(user=self.owner, organization=self.org, role='owner').delete()
        OrganizationAccess.objects.create(user=self.other, organization=self.org, role='owner')
        self.assertFalse(can_read_call(self.user, call))

    def test_legacy_internal_participants_and_owner_are_visible(self):
        call = self.call(employee=self.user, peer_employee=self.other, direction='internal')
        PhoneCall.objects.filter(pk=call.pk).update(classification_evidence={})
        self.assertTrue(can_read_call(self.user, call))
        self.assertTrue(can_read_call(self.other, call))

    def test_tasks_and_operations_api_cannot_bypass_owner_context(self):
        from pool_service.communication_models import CallAnalysis
        from pool_service.services.task_read_permissions import readable_tasks
        from pool_service.operations_mcp_views import _get_call_analysis
        call = self.call()
        CallAnalysis.objects.create(call=call, status='ready', transcript='Synthetic protected conversation')
        with self.assertRaises(ValueError):
            _get_call_analysis(self.org, {'call_id': call.pk}, actor=self.user)
        task = ServiceTask.objects.create(organization=self.org, created_by=self.user,
            title='Synthetic protected follow-up', start_date=timezone.localdate(),
            payload_json={'source_call_id': call.pk})
        self.assertEqual(readable_tasks(ServiceTask.objects.filter(pk=task.pk), self.user), [])
        self.assertFalse(self.http.get(reverse('task_edit', args=[task.pk])).status_code == 200)


from pool_service.tests.test_crm_inbox import Fixture
from django.test import override_settings
from pool_service.crm_inbox_models import CrmInboxEvent


@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class InternalWebhookControlTests(Fixture, TestCase):
    def setUp(self):
        super().setUp()
        self.megafon()
        self.owner = get_user_model().objects.create_user('synthetic-internal-owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        self.first = Employee.objects.create(organization=self.org, user=self.owner, display_name='Synthetic first')
        self.second = Employee.objects.create(organization=self.org, display_name='Synthetic second')
        self.assignment = assign_phone(actor=self.owner, employee=self.first, connection=self.telephony,
            extension='101', valid_from=self.now-timedelta(days=2))

    def test_repeated_external_webhook_retains_verified_internal_call_without_lead_intake(self):
        assign_phone(actor=self.owner, employee=self.second, connection=self.telephony,
            phone_key='9001234567', valid_from=self.now-timedelta(days=2))
        self.assertEqual(self.history(ext='101').status_code, 200)
        self.assertEqual(self.history(ext='101', duration='25').status_code, 200)
        call = PhoneCall.objects.get()
        self.assertEqual(call.direction, 'internal')
        self.assertEqual(call.classification_evidence['kind'], 'internal')
        self.assertIsNone(call.client_id)
        self.assertFalse(CrmInboxEvent.objects.exists())

    def test_two_confirmed_numbers_of_same_employee_are_internal(self):
        assign_phone(actor=self.owner, employee=self.first, connection=self.telephony,
            phone_key='9001234567', valid_from=self.now-timedelta(days=2))
        self.assertEqual(self.history(ext='101').status_code, 200)
        call = PhoneCall.objects.get()
        self.assertEqual(call.direction, 'internal')
        self.assertEqual(call.employee_profile_id, call.peer_employee_profile_id)
        self.assertFalse(CrmInboxEvent.objects.exists())


@override_settings(COMMUNICATION_RECORDING_DOWNLOAD_INLINE=False, COMMUNICATION_CLIENT_ENRICHMENT_ENABLED=False)
class OwnerAssignmentIntegrationTests(Fixture, TestCase):
    def test_real_owner_source_can_be_assigned_and_transferred_without_preflight_disclosure(self):
        from pool_service.crm_commercial_models import CrmIntakeActivation, CrmLead
        from pool_service.services.crm_inbox import claim_one, deliver_claim
        from pool_service.services.crm_lead_intake import consume_incoming
        from pool_service.services.crm_lead_access import can_read_lead
        from pool_service.services.crm_assignments import responsible_candidates, execute_assignment
        self.org.crm_v2_read = self.org.crm_v2_write = self.org.crm_intake_process = True
        self.org.paid_until = self.now + timedelta(days=30)
        self.org.save()
        CrmIntakeActivation.objects.create(organization=self.org, starts_at=self.now-timedelta(minutes=30))
        owner = get_user_model().objects.create_user('synthetic-assignment-owner')
        old = get_user_model().objects.create_user('synthetic-old-responsible')
        new = get_user_model().objects.create_user('synthetic-new-responsible')
        for user, role in [(owner, 'owner'), (old, 'manager'), (new, 'manager')]:
            OrganizationAccess.objects.create(organization=self.org, user=user, role=role)
            CrmAccess.objects.create(organization=self.org, user=user, view_leads=True,
                create_own_leads=True, take_leads=True, edit_own_leads=True, transition_own_leads=True)
            CommunicationAccess.objects.filter(organization=self.org, user=user).update(
                can_view_all_calls=True, can_view_own_calls=True, can_listen_calls=True)
        self.megafon()
        employee = Employee.objects.create(organization=self.org, user=owner, display_name='Synthetic caller')
        from pool_service.communication_models import TelephonyEmployeeIdentity
        TelephonyEmployeeIdentity.objects.create(organization=self.org, connection=self.telephony,
            extension='101', employee=employee, status='manually_matched',
            confirmed_by=owner, confirmed_at=self.now)
        self.assertEqual(self.history(ext='101', status='missed').status_code, 200)
        self.assertEqual(deliver_claim(*claim_one(**self.scope('call')), consumer=consume_incoming), 'done')
        call, lead = PhoneCall.objects.get(), CrmLead.objects.get()
        self.assertFalse(can_read_call(old, call))
        self.assertFalse(can_read_lead(old, self.org, lead))
        candidates = responsible_candidates(actor=owner, organization=self.org, kind='lead', subject_id=lead.pk)
        self.assertIn(old.pk, [row['id'] for row in candidates['users']])
        self.assertFalse(can_read_lead(old, self.org, lead))
        execute_assignment(actor=owner, organization=self.org, operation_key='assign-owner-source',
            command_type='lead.assign', subject_id=lead.pk, expected_revision=lead.revision,
            changes={'responsible_id': old.pk})
        lead.refresh_from_db()
        self.assertTrue(can_read_call(old, call, media=True))
        self.assertFalse(can_read_call(new, call))
        execute_assignment(actor=old, organization=self.org, operation_key='transfer-owner-source',
            command_type='lead.assign', subject_id=lead.pk, expected_revision=lead.revision,
            changes={'responsible_id': new.pk})
        self.assertFalse(can_read_call(old, call, media=True))
        self.assertTrue(can_read_call(new, call, media=True))
