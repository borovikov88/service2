import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from pool_service.crm_commercial_models import CrmAccess
from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.staff_call_models import StaffAccount
from pool_service.services import crm_role_preview_snapshot as adapter
from pool_service.services.crm_role_policy_preview import CAPABILITIES


class CrmRolePreviewSnapshotTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='PRIVATE-ORG', crm_v2_read=True, crm_v2_write=True)
        self.actor = get_user_model().objects.create_user('PRIVATE-OWNER')
        self.target = get_user_model().objects.create_user('PRIVATE-MEMBER', email='private@example.invalid')
        OrganizationAccess.objects.create(organization=self.org, user=self.actor, role='owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.target, role='manager')
        StaffAccount.objects.create(organization=self.org, user=self.target, kind='person')

    def preview(self, **changes):
        kwargs = dict(actor_id=self.actor.pk, organization_id=self.org.pk,
            target_id=self.target.pk, proposed_roles=['manager'])
        kwargs.update(changes)
        return adapter.preview_member_crm_roles(**kwargs)

    def test_missing_grant_preview_without_write_or_pii(self):
        with CaptureQueriesContext(connection) as captured:
            result = self.preview()
        self.assertTrue(all(row['sql'].lstrip().upper().startswith('SELECT') for row in captured))
        projections = '\n'.join(row['sql'].lower().split(' from ')[0] for row in captured)
        for field in ('email', 'password', 'username', 'first_name', 'last_name'):
            self.assertNotIn(field, projections)
        self.assertEqual(set(result['increases']), set(CAPABILITIES))
        self.assertFalse(result['preview']['authorizes_change'])
        self.assertFalse(CrmAccess.objects.exists())
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('private@example', json.dumps(result))

    def test_downgrade_reveals_legacy_removal_without_applying(self):
        grant = CrmAccess.objects.create(organization=self.org, user=self.target,
            **dict.fromkeys(CAPABILITIES, True))
        result = self.preview(proposed_roles=['service'])
        self.assertEqual(set(result['decreases']), set(CAPABILITIES))
        grant.refresh_from_db()
        self.assertTrue(grant.view_leads)
        self.assertTrue(OrganizationAccess.objects.filter(user=self.target, role='manager').exists())

    def test_manager_and_foreign_actor_denied(self):
        other = get_user_model().objects.create_user('OTHER')
        for actor in (self.target.pk, other.pk):
            with self.subTest(actor=actor), self.assertRaisesMessage(PermissionDenied, adapter.UNAVAILABLE):
                self.preview(actor_id=actor)

    def test_admin_allowed(self):
        admin = get_user_model().objects.create_user('ADMIN')
        OrganizationAccess.objects.create(organization=self.org, user=admin, role='admin')
        self.assertEqual(self.preview(actor_id=admin.pk)['target_id'], self.target.pk)

    def test_superuser_actor_and_target_rejected(self):
        for user in (self.actor, self.target):
            user.is_superuser = True
            user.save(update_fields=['is_superuser'])
            with self.assertRaisesMessage(PermissionDenied, adapter.UNAVAILABLE):
                self.preview()
            user.is_superuser = False
            user.save(update_fields=['is_superuser'])

    def test_dismissed_target_no_proposed_permission(self):
        Employee.objects.create(organization=self.org, user=self.target, display_name='PRIVATE-EMPLOYEE',
            employment_status=Employee.STATUS_DISMISSED)
        self.assertFalse(self.preview()['increases'])

    def test_no_owner_changes_self_promotion_or_absent_members(self):
        for changes in ({'target_id': self.actor.pk}, {'proposed_roles': ['owner']},
                        {'target_id': 999999}, {'proposed_roles': []}, {'proposed_roles': ['invalid']}):
            with self.subTest(changes=changes), self.assertRaisesMessage(PermissionDenied, adapter.UNAVAILABLE):
                self.preview(**changes)

    def test_account_kind_requires_explicit_scope(self):
        account = StaffAccount.objects.get(user=self.target, organization=self.org)
        account.kind = 'service'
        account.save(update_fields=['kind'])
        result = self.preview()
        self.assertEqual(result['preview']['rows'], ())
        self.assertFalse(result['increases'])

    def test_changed_snapshot_fails_closed(self):
        before = adapter._read_snapshot(self.actor.pk, self.org.pk, self.target.pk)
        after = dict(before, crm_write=False)
        with patch.object(adapter, '_read_snapshot', side_effect=[before, after]):
            with self.assertRaisesMessage(PermissionDenied, adapter.UNAVAILABLE):
                self.preview()

    def test_command_sanitized_no_partial_output(self):
        out = StringIO()
        with patch.object(adapter, '_read_snapshot', side_effect=RuntimeError('PRIVATE-DB-ERROR')):
            with self.assertRaisesMessage(CommandError, adapter.UNAVAILABLE):
                call_command('preview_crm_roles', actor=self.actor.pk, organization=self.org.pk,
                    target_user=self.target.pk, roles=['manager'], stdout=out)
        self.assertEqual(out.getvalue(), '')

    def test_command_success_never_applies(self):
        out = StringIO()
        call_command('preview_crm_roles', actor=self.actor.pk, organization=self.org.pk,
            target_user=self.target.pk, roles=['manager'], stdout=out)
        result = json.loads(out.getvalue())
        self.assertFalse(result['preview']['authorizes_change'])
        self.assertFalse(CrmAccess.objects.exists())
