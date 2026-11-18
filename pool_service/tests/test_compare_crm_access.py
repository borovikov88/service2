import json
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from pool_service.crm_commercial_models import CrmAccess
from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.management.commands import compare_crm_access as diagnostic


class CompareCrmAccessTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Synthetic', crm_v2_read=True, crm_v2_write=True)
        self.actor = get_user_model().objects.create_user('operator')
        self.left = get_user_model().objects.create_user('PRIVATE-LEFT', email='private@example.invalid')
        self.right = get_user_model().objects.create_user('PRIVATE-RIGHT')
        for user, role in ((self.actor, 'owner'), (self.left, 'manager'), (self.right, 'manager')):
            OrganizationAccess.objects.create(organization=self.org, user=user, role=role)
        CrmAccess.objects.create(organization=self.org, user=self.right, view_leads=True,
            create_own_deals=True, manage_own_next_action=True)

    def read(self, **changes):
        out = StringIO()
        options = dict(organization=self.org.pk, actor=self.actor.pk,
            left_user=self.left.pk, right_user=self.right.pk, stdout=out)
        options.update(changes)
        call_command('compare_crm_access', **options)
        return json.loads(out.getvalue())

    def denied(self, **changes):
        out = StringIO()
        with self.assertRaisesMessage(CommandError, diagnostic.UNAVAILABLE):
            self.read(stdout=out, **changes)
        self.assertEqual(out.getvalue(), '')

    def test_same_role_different_grants_no_writes_no_pii(self):
        with CaptureQueriesContext(connection) as captured:
            result = self.read()
        self.assertTrue(all(row['sql'].lstrip().upper().startswith('SELECT') for row in captured))
        left, right = result['users']
        self.assertEqual(left['roles'], right['roles'])
        self.assertFalse(left['can_enter_leads'])
        self.assertTrue(right['can_enter_leads'])
        self.assertTrue(left['can_enter_deals'])
        self.assertFalse(left['own_deal_create_flags_enabled'])
        self.assertTrue(right['own_deal_create_flags_enabled'])
        self.assertEqual(set(result['different_crm_flags']),
            {'view_leads', 'create_own_deals', 'manage_own_next_action'})
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('private@example', json.dumps(result))
        self.assertEqual(CrmAccess.objects.count(), 1)

    def test_manager_cannot_read_others(self):
        self.denied(actor=self.left.pk)

    def test_superuser_does_not_bypass(self):
        self.actor.is_superuser = True
        self.actor.save(update_fields=['is_superuser'])
        self.denied()

    def test_inactive_or_dismissed_actor_denied(self):
        self.actor.is_active = False
        self.actor.save(update_fields=['is_active'])
        self.denied()
        self.actor.is_active = True
        self.actor.save(update_fields=['is_active'])
        Employee.objects.create(organization=self.org, user=self.actor, display_name='PRIVATE',
            employment_status=Employee.STATUS_DISMISSED)
        self.denied()

    def test_foreign_or_missing_member_same_error(self):
        outsider = get_user_model().objects.create_user('OUTSIDE')
        self.denied(right_user=outsider.pk)
        self.denied(right_user=999999)
        self.denied(organization=999999)

    def test_invalid_input(self):
        self.denied(left_user=self.right.pk)
        self.denied(actor=-1)
        self.denied(left_user=2**63)

    def test_disabled_org_and_inactive_target_are_reported(self):
        self.org.crm_v2_read = self.org.crm_v2_write = False
        self.org.save(update_fields=['crm_v2_read', 'crm_v2_write'])
        self.right.is_active = False
        self.right.save(update_fields=['is_active'])
        result = self.read()
        self.assertFalse(result['users'][1]['active'])
        self.assertFalse(result['users'][1]['can_enter_leads'])
        self.assertFalse(result['users'][1]['can_enter_deals'])
        self.assertFalse(result['users'][1]['own_deal_create_flags_enabled'])

    def test_recheck_revocation_no_partial_output(self):
        with patch.object(diagnostic, '_scope', side_effect=[self.org, CommandError('PRIVATE-ERROR')]):
            self.denied()

    def test_unexpected_error_redacted(self):
        with patch.object(diagnostic, 'can_view_leads', side_effect=RuntimeError('PRIVATE-DB-ERROR')):
            self.denied()
