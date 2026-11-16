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
        with self.assertRaises(ValidationError):
            set_work_context(actor=self.owner, call_id=call.pk, lead_id=lead.pk)
        self.assertFalse(CallWorkContext.objects.exists())

    def test_employee_cannot_delegate_owner_conversation(self):
        call, lead = self.call(), self.create()
        with self.assertRaises(PermissionDenied):
            set_work_context(actor=self.user, call_id=call.pk, lead_id=lead.pk)

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

    def test_only_owner_can_return_held_history(self):
        call = self.call()
        hold = CallPrivacyHold.objects.create(call=call, organization=self.org, owner=self.owner)
        response = self.http.post(reverse('communication_personal_history'), {'hold': hold.pk})
        self.assertEqual(response.status_code, 403)
        hold.refresh_from_db()
        self.assertIsNone(hold.released_at)

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

    def test_overlap_shared_and_cross_connection_do_not_guess_internal(self):
        now = timezone.now()
        assign_phone(actor=self.owner, employee=self.employee, connection=self.telephony, extension='101', valid_from=now)
        with self.assertRaises(ValidationError):
            assign_phone(actor=self.owner, employee=self.employee, connection=self.telephony, extension='101', valid_from=now)
        EmployeeTelephoneAssignment.objects.create(organization=self.org, connection=self.telephony, shared=True,
            phone_key='9990001122', valid_from=now, confirmed_by=self.owner)
        call = self.call(employee=self.user, provider_extension='101', started_at=now)
        self.assertEqual(call.direction, 'in')

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
