from datetime import timedelta
from io import StringIO
import os
from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from pool_service.communication_models import CommunicationAccess, PhoneCall, TelephonyConnection
from pool_service.models import DataAuditLog, Employee, Organization, OrganizationAccess
from pool_service.services.call_access import can_read_call
from pool_service.services.call_processing_settings import settings_allowed
from pool_service.services.organization_ownership import OwnershipBlocked, establish_first_owner
from pool_service.staff_call_models import StaffAccount


class OrganizationOwnershipTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Synthetic first-owner organization',
            plan_type='company_paid', paid_until=timezone.now()+timedelta(days=30))
        self.foreign = Organization.objects.create(name='Synthetic independent organization')
        self.candidate = get_user_model().objects.create_user('synthetic-first-owner')
        self.employee = Employee.objects.create(organization=self.org, user=self.candidate,
            display_name='Synthetic human administrator')
        OrganizationAccess.objects.create(organization=self.org, user=self.candidate, role='admin')
        self.staff = get_user_model().objects.create_user('synthetic-working-colleague')
        OrganizationAccess.objects.create(organization=self.org, user=self.staff, role='manager')
        for user in [self.candidate, self.staff]:
            CommunicationAccess.objects.update_or_create(organization=self.org, user=user,
                defaults={'can_view_all_calls': True, 'can_view_own_calls': True, 'can_listen_calls': True})
        self.line = TelephonyConnection.objects.create(organization=self.org, external_id='synthetic-owner-line')
        self.call = PhoneCall.objects.create(organization=self.org, connection=self.line,
            employee=self.candidate, external_id='synthetic-old-owner-call', direction='out',
            phone_number='+79990001122', started_at=timezone.now(), recording_ref='synthetic-audio')
        self.context = dict(organization_id=self.org.pk, user_id=self.candidate.pk,
            expected_sha='a'*40, run_id='123', run_attempt='1')

    def apply(self, **changes):
        values = {**self.context, **changes}
        plan = establish_first_owner(**values)['plan_digest']
        return establish_first_owner(**values, apply=True, expected_plan=plan)

    def test_preview_is_read_only_and_does_not_claim_an_administrator_is_owner(self):
        result = establish_first_owner(**self.context)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['eligible_calls'], 1)
        self.assertFalse(OrganizationAccess.objects.filter(role='owner').exists())
        self.assertFalse(settings_allowed(self.candidate, self.org))
        self.assertFalse(DataAuditLog.objects.filter(entity_type='OrganizationOwnership').exists())
        self.call.refresh_from_db()
        self.assertIsNone(self.call.owner_user_snapshot)

    def test_explicit_owner_changes_live_access_and_preserves_call_content_and_other_roles(self):
        self.assertTrue(can_read_call(self.staff, self.call, media=True))
        original = (self.call.phone_number, self.call.direction, self.call.recording_ref, self.call.started_at)
        result = self.apply()
        self.assertEqual((result['status'], result['roles_added'], result['snapshot_count']), ('applied', 1, 1))
        self.assertTrue(settings_allowed(self.candidate, self.org))
        self.assertTrue(can_read_call(self.candidate, self.call, media=True))
        self.assertFalse(can_read_call(self.staff, self.call, media=True))
        self.assertEqual(set(OrganizationAccess.objects.filter(organization=self.org,
            user=self.candidate).values_list('role', flat=True)), {'admin', 'owner'})
        self.call.refresh_from_db()
        self.assertEqual(self.call.owner_user_snapshot, self.candidate.pk)
        self.assertEqual((self.call.phone_number, self.call.direction, self.call.recording_ref, self.call.started_at), original)
        audit = DataAuditLog.objects.get(pk=result['audit_id'])
        self.assertIsNone(audit.actor_id)
        self.assertEqual(audit.after['source'], 'protected_first_owner_workflow')
        self.assertEqual(PhoneCall.objects.count(), 1)

    def test_other_organization_and_an_existing_history_floor_are_preserved(self):
        OrganizationAccess.objects.create(organization=self.foreign, user=self.candidate, role='admin')
        foreign_line = TelephonyConnection.objects.create(organization=self.foreign, external_id='synthetic-foreign-line')
        foreign_call = PhoneCall.objects.create(organization=self.foreign, connection=foreign_line,
            employee=self.candidate, external_id='synthetic-foreign-call', started_at=timezone.now())
        PhoneCall.objects.filter(pk=self.call.pk).update(owner_user_snapshot=self.staff.pk)
        result = self.apply()
        self.assertEqual(result['snapshot_count'], 0)
        self.call.refresh_from_db(); foreign_call.refresh_from_db()
        self.assertEqual(self.call.owner_user_snapshot, self.staff.pk)
        self.assertIsNone(foreign_call.owner_user_snapshot)
        self.assertFalse(OrganizationAccess.objects.filter(organization=self.foreign, role='owner').exists())

    def test_historical_floor_backfill_covers_more_than_one_batch(self):
        PhoneCall.objects.bulk_create([PhoneCall(organization=self.org, connection=self.line,
            employee=self.candidate, external_id=f'synthetic-backfill-{index}', direction='in',
            phone_number='+79990001122', started_at=timezone.now()) for index in range(201)])
        result = self.apply()
        self.assertEqual(result['snapshot_count'], 202)
        self.assertEqual(PhoneCall.objects.filter(organization=self.org,
            owner_user_snapshot=self.candidate.pk).count(), 202)

    def test_retry_does_not_duplicate_role_or_audit(self):
        plan = establish_first_owner(**self.context)['plan_digest']
        first = establish_first_owner(**self.context, apply=True, expected_plan=plan)
        again = establish_first_owner(**self.context, apply=True, expected_plan=plan)
        self.assertEqual(again['status'], 'already_owner')
        self.assertEqual(again['roles_added'], 0)
        self.assertEqual(again['snapshot_count'], 0)
        self.assertIsNone(again['audit_id'])
        self.assertTrue(DataAuditLog.objects.filter(pk=first['audit_id']).exists())
        self.assertEqual(OrganizationAccess.objects.filter(organization=self.org, role='owner').count(), 1)
        self.assertEqual(DataAuditLog.objects.filter(entity_type='OrganizationOwnership').count(), 1)

    def test_another_owner_is_never_replaced(self):
        OrganizationAccess.objects.create(organization=self.org, user=self.staff, role='owner')
        with self.assertRaisesMessage(OwnershipBlocked, 'owner_already_exists'):
            self.apply()
        self.assertFalse(OrganizationAccess.objects.filter(user=self.candidate, role='owner').exists())

    def test_changed_identity_invalidates_reviewed_plan(self):
        plan = establish_first_owner(**self.context)['plan_digest']
        get_user_model().objects.filter(pk=self.candidate.pk).update(username='synthetic-renamed-account')
        with self.assertRaisesMessage(OwnershipBlocked, 'plan_changed'):
            establish_first_owner(**self.context, apply=True, expected_plan=plan)
        self.assertFalse(OrganizationAccess.objects.filter(role='owner').exists())

    def test_preview_from_another_commit_cannot_authorize_a_new_owner(self):
        plan = establish_first_owner(**self.context)['plan_digest']
        with self.assertRaisesMessage(OwnershipBlocked, 'plan_changed'):
            establish_first_owner(**{**self.context, 'expected_sha':'b'*40}, apply=True, expected_plan=plan)
        self.assertFalse(OrganizationAccess.objects.filter(role='owner').exists())

    def test_old_completed_receipt_does_not_authorize_another_deployed_version(self):
        plan = establish_first_owner(**self.context)['plan_digest']
        establish_first_owner(**self.context, apply=True, expected_plan=plan)
        with self.assertRaisesMessage(OwnershipBlocked, 'plan_changed'):
            establish_first_owner(**{**self.context, 'expected_sha':'b'*40}, apply=True, expected_plan=plan)
        self.assertEqual(DataAuditLog.objects.filter(entity_type='OrganizationOwnership').count(), 1)

    def test_inactive_dismissed_service_system_superuser_and_unlinked_accounts_are_rejected(self):
        for case in ['inactive', 'dismissed', 'service', 'system', 'superuser', 'unlinked', 'manager', 'foreign']:
            with self.subTest(case=case):
                user = get_user_model().objects.create_user('synthetic-rejected-'+case,
                    is_active=case != 'inactive', is_superuser=case == 'superuser')
                company = self.foreign if case == 'foreign' else self.org
                OrganizationAccess.objects.create(organization=company, user=user,
                    role='manager' if case == 'manager' else 'admin')
                if case != 'unlinked':
                    Employee.objects.create(organization=company, user=user, display_name='Synthetic rejected candidate',
                        employment_status='dismissed' if case == 'dismissed' else 'active')
                if case in {'service', 'system'}:
                    StaffAccount.objects.create(organization=company, user=user, kind=case)
                with self.assertRaises(OwnershipBlocked):
                    self.apply(user_id=user.pk)
                self.assertFalse(OrganizationAccess.objects.filter(user=user, role='owner').exists())

    def test_audit_failure_rolls_back_owner_and_history_metadata(self):
        plan = establish_first_owner(**self.context)['plan_digest']
        with patch('pool_service.services.organization_ownership.DataAuditLog.objects.create', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                establish_first_owner(**self.context, apply=True, expected_plan=plan)
        self.assertFalse(OrganizationAccess.objects.filter(role='owner').exists())
        self.call.refresh_from_db()
        self.assertIsNone(self.call.owner_user_snapshot)

    def test_command_requires_protected_environment_exact_head_and_apply_plan(self):
        kwargs = dict(organization_id=self.org.pk, user_id=self.candidate.pk,
            expected_sha='a'*40, run_id='123', run_attempt='1')
        with patch.dict(os.environ, {'SERVICE2_OWNER_BOOTSTRAP': ''}):
            with self.assertRaisesMessage(CommandError, 'owner_protected_workflow_required'):
                call_command('establish_organization_owner', **kwargs)
        with patch.dict(os.environ, {'SERVICE2_OWNER_BOOTSTRAP': '1'}):
            with self.assertRaisesMessage(CommandError, 'owner_reviewed_plan_required'):
                call_command('establish_organization_owner', apply=True, **kwargs)
            with patch('pool_service.management.commands.establish_organization_owner.subprocess.run',
                    return_value=SimpleNamespace(stdout='b'*40)):
                with self.assertRaisesMessage(CommandError, 'owner_commit_mismatch'):
                    call_command('establish_organization_owner', **kwargs)
            output = StringIO()
            with patch('pool_service.management.commands.establish_organization_owner.subprocess.run',
                    return_value=SimpleNamespace(stdout='a'*40)):
                call_command('establish_organization_owner', stdout=output, **kwargs)
            self.assertIn('ORG_OWNER_BOOTSTRAP_V1', output.getvalue())
            self.assertNotIn(self.candidate.username, output.getvalue())
        self.assertFalse(OrganizationAccess.objects.filter(role='owner').exists())
