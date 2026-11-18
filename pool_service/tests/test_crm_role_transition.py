from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from pool_service.models import Organization, OrganizationAccess, DataAuditLog, Employee
from pool_service.staff_call_models import StaffAccount
from pool_service.crm_commercial_models import CrmAccess
from pool_service.crm_role_models import CrmRolePolicy
from pool_service.communication_models import CommunicationAccess
from pool_service.services import crm_role_transition as service
from pool_service.services.crm_role_policy_preview import CAPABILITIES


@override_settings(CRM_ROLE_TRANSITION_ENABLED=True)
class RoleTransitionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Synthetic', crm_v2_read=True, crm_v2_write=True)
        self.owner = get_user_model().objects.create_user('owner')
        self.target = get_user_model().objects.create_user('member')
        OrganizationAccess.objects.create(organization=self.org, user=self.owner, role='owner')
        OrganizationAccess.objects.create(organization=self.org, user=self.target, role='service')
        StaffAccount.objects.create(organization=self.org, user=self.target, kind='person')
        self.ids = dict(actor_id=self.owner.pk, organization_id=self.org.pk, target_id=self.target.pk)

    def args(self, roles=('manager',), overrides=None, approved=None):
        return dict(**self.ids, proposed_roles=roles, acknowledge_assigned_call_visibility=True,
            overrides=overrides or dict.fromkeys(CAPABILITIES, 'inherit'),
            approved_increases=list(CAPABILITIES) if approved is None else approved,
            expected_digest=service.prepare_role_transition(**self.ids)['expected_digest'])

    def test_owner_changes_role_and_crm_atomically_with_audit(self):
        result = service.apply_role_transition(**self.args())
        self.assertTrue(result['changed'])
        self.assertEqual(list(OrganizationAccess.objects.filter(user=self.target).values_list('role', flat=True)), ['manager'])
        grant = CrmAccess.objects.get(user=self.target)
        self.assertTrue(all(getattr(grant, f) for f in CAPABILITIES))
        self.assertEqual(StaffAccount.objects.get(user=self.target).revision, 1)
        audit = DataAuditLog.objects.get(entity_type='crm_role_transition_v1')
        self.assertEqual(audit.actor_id, self.owner.pk)
        self.assertEqual(audit.before['roles'], ['service'])
        self.assertEqual(audit.after['roles'], ['manager'])
        self.assertNotIn('member', str(audit.after))
        comm = CommunicationAccess.objects.get(user=self.target)
        self.assertFalse(any(getattr(comm, f) for f in service.COMM_FIELDS))

    def test_disabled_by_default(self):
        with override_settings(CRM_ROLE_TRANSITION_ENABLED=False), self.assertRaises(PermissionDenied):
            service.apply_role_transition(**self.args())
        self.assertFalse(CrmAccess.objects.filter(user=self.target).exists())

    def test_stale_snapshot_cannot_repeat_or_restore_revoked_grant(self):
        args = self.args()
        service.apply_role_transition(**args)
        with self.assertRaises(ValidationError):
            service.apply_role_transition(**args)
        self.assertEqual(DataAuditLog.objects.count(), 1)
        args = self.args(approved=[])
        grant = CrmAccess.objects.get(user=self.target); grant.view_leads = False; grant.save(update_fields=['view_leads'])
        with self.assertRaises(ValidationError):
            service.apply_role_transition(**args)
        self.assertFalse(CrmAccess.objects.get(user=self.target).view_leads)

    def test_explicit_deny_preserved_and_exact_increase_confirmation(self):
        overrides = dict.fromkeys(CAPABILITIES, 'inherit')
        overrides['view_leads'] = 'deny'
        args = self.args(overrides=overrides, approved=[f for f in CAPABILITIES if f != 'view_leads'])
        service.apply_role_transition(**args)
        self.assertFalse(CrmAccess.objects.get(user=self.target).view_leads)
        with self.assertRaises(ValidationError):
            service.apply_role_transition(**self.args(approved=['view_leads', 'create_own_deals']))

    def test_missing_review_or_extra_confirmation_fails_before_mutation(self):
        for change in ({'overrides': {}}, {'approved_increases': []},
                       {'overrides': dict.fromkeys(CAPABILITIES, 'bad')},
                       {'proposed_roles': ['admin']}, {'proposed_roles': ['accountant']},
                       {'proposed_roles': ['owner']}, {'proposed_roles': []}):
            args = self.args(); args.update(change)
            with self.assertRaises(ValidationError):
                service.apply_role_transition(**args)
        self.assertFalse(DataAuditLog.objects.exists())
        self.assertFalse(CrmAccess.objects.filter(user=self.target).exists())

    def test_managed_manager_deal_read_deny_overrides_legacy_implicit_role(self):
        from pool_service.services.crm_commercial_access import can_view_team_deals
        overrides = dict.fromkeys(CAPABILITIES, 'inherit'); overrides['view_team_deals'] = 'deny'
        service.apply_role_transition(**self.args(overrides=overrides, approved=[f for f in CAPABILITIES if f != 'view_team_deals']))
        self.assertFalse(can_view_team_deals(self.target, self.org))

    def test_downgrade_removes_inherited_flags_and_communication(self):
        service.apply_role_transition(**self.args())
        CommunicationAccess.objects.filter(user=self.target).update(can_view_all_calls=True, can_listen_calls=True)
        result = service.apply_role_transition(**self.args(roles=('installer',), approved=[]))
        self.assertTrue(result['changed'])
        self.assertFalse(any(getattr(CrmAccess.objects.get(user=self.target), f) for f in CAPABILITIES))
        self.assertFalse(any(getattr(CommunicationAccess.objects.get(user=self.target), f) for f in service.COMM_FIELDS))

    def test_enrollment_is_audited_then_identical_noop_does_not_write(self):
        first = service.apply_role_transition(**self.args(roles=('service',), approved=[]))
        self.assertTrue(first['changed'])
        result = service.apply_role_transition(**self.args(roles=('service',), approved=[]))
        self.assertFalse(result['changed'])
        self.assertEqual(DataAuditLog.objects.count(), 1)
        self.assertEqual(CrmRolePolicy.objects.get(user=self.target).revision, 1)

    def test_audit_failure_rolls_back_signals_roles_grants_revision(self):
        args = self.args()
        before_comm = CommunicationAccess.objects.filter(user=self.target).values().get()
        with patch.object(DataAuditLog.objects, 'create', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                service.apply_role_transition(**args)
        self.assertEqual(list(OrganizationAccess.objects.filter(user=self.target).values_list('role', flat=True)), ['service'])
        self.assertFalse(CrmAccess.objects.filter(user=self.target).exists())
        self.assertFalse(CrmRolePolicy.objects.filter(user=self.target).exists())
        self.assertEqual(StaffAccount.objects.get(user=self.target).revision, 0)
        self.assertEqual(CommunicationAccess.objects.filter(user=self.target).values().get(), before_comm)

    def test_owner_revoked_after_prepare_is_denied(self):
        args = self.args()
        OrganizationAccess.objects.filter(user=self.owner).delete()
        with self.assertRaises(PermissionDenied):
            service.apply_role_transition(**args)

    def test_private_and_source_records_are_untouched(self):
        # Inspect SQL against a synthetic database; no call/private/source writes.
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        args = self.args()
        with CaptureQueriesContext(connection) as capture:
            service.apply_role_transition(**args)
        writes = '\n'.join(x['sql'].lower() for x in capture if x['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')))
        for table in ('phonecall', 'callprivatenumber', 'callprivacyhold', 'crmleadsource', 'crmdeal', 'crmlead'):
            self.assertNotIn(connection.ops.quote_name('pool_service_' + table).lower(), writes)

    def test_service_unknown_inactive_dismissed_superuser_targets_denied(self):
        for kind in ('unknown', 'service', 'system'):
            StaffAccount.objects.filter(user=self.target).update(kind=kind)
            with self.assertRaises(PermissionDenied): service.prepare_role_transition(**self.ids)
        StaffAccount.objects.filter(user=self.target).update(kind='person')
        self.target.is_active = False; self.target.save()
        with self.assertRaises(PermissionDenied): service.prepare_role_transition(**self.ids)
        self.target.is_active = True; self.target.is_superuser = True; self.target.save()
        with self.assertRaises(PermissionDenied): service.prepare_role_transition(**self.ids)
        self.target.is_superuser = False; self.target.save()
        Employee.objects.create(organization=self.org, user=self.target, display_name='Synthetic', employment_status='dismissed')
        with self.assertRaises(PermissionDenied): service.prepare_role_transition(**self.ids)

    def test_self_foreign_admin_actor_rejected(self):
        with self.assertRaises(PermissionDenied):
            service.prepare_role_transition(**{**self.ids, 'target_id': self.owner.pk})
        other = Organization.objects.create(name='Other')
        with self.assertRaises(PermissionDenied):
            service.prepare_role_transition(**{**self.ids, 'organization_id': other.pk})
        OrganizationAccess.objects.filter(user=self.owner).update(role='admin')
        with self.assertRaises(PermissionDenied): service.prepare_role_transition(**self.ids)

    def test_crm_read_increase_requires_call_visibility_acknowledgment(self):
        args = self.args(); args['acknowledge_assigned_call_visibility'] = False
        with self.assertRaises(ValidationError):
            service.apply_role_transition(**args)
        self.assertFalse(DataAuditLog.objects.exists())

    def test_read_grant_preserves_private_hold_and_owner_assignment_gates(self):
        from django.utils import timezone
        from pool_service.communication_models import PhoneCall, TelephonyConnection
        from pool_service.crm_commercial_models import CrmLead
        from pool_service.staff_call_models import CallWorkContext, CallPrivacyHold
        from pool_service.call_processing_models import CallPrivateNumber
        from pool_service.services.call_access import can_read_call
        OrganizationAccess.objects.filter(user=self.target).delete()
        OrganizationAccess.objects.create(organization=self.org, user=self.target, role='manager')
        CommunicationAccess.objects.filter(user=self.target).update(can_view_own_calls=True, can_listen_calls=True)
        connection = TelephonyConnection.objects.create(organization=self.org, external_id='synthetic')
        call = PhoneCall.objects.create(organization=self.org, connection=connection, employee=self.owner,
            external_id='synthetic-call', direction='in', phone_number='+79990001122', started_at=timezone.now())
        lead = CrmLead.objects.create(organization=self.org, assignee=self.target, title='Synthetic')
        context = CallWorkContext.objects.create(organization=self.org, call=call, lead=lead, changed_by=self.owner)
        self.assertFalse(can_read_call(self.target, call, media=True))
        service.apply_role_transition(**self.args())
        self.assertTrue(can_read_call(self.target, call, media=True))
        lead.assignee = self.owner; lead.save()
        self.assertFalse(can_read_call(self.target, call, media=True))
        lead.assignee = self.target; lead.save()
        hold = CallPrivacyHold.objects.create(organization=self.org, call=call, owner=self.owner)
        self.assertFalse(can_read_call(self.target, call, media=True))
        hold.released_at = timezone.now(); hold.save()
        CallPrivateNumber.objects.create(organization=self.org, owner=self.owner,
            phone_key='9990001122', label='Synthetic private')
        self.assertFalse(can_read_call(self.target, call, media=True))


    def test_durable_deny_survives_role_change_when_reviewed_map_is_reused(self):
        overrides = dict.fromkeys(CAPABILITIES, 'inherit'); overrides['view_leads'] = 'deny'
        service.apply_role_transition(**self.args(overrides=overrides, approved=[f for f in CAPABILITIES if f != 'view_leads']))
        policy = CrmRolePolicy.objects.get(user=self.target)
        self.assertEqual(policy.overrides['view_leads'], 'deny')
        self.assertEqual(policy.reviewed_by_id, self.owner.pk)
        service.apply_role_transition(**self.args(roles=('service',), overrides=policy.overrides, approved=[]))
        policy.refresh_from_db(); self.assertEqual(policy.revision, 2)
        self.assertEqual(policy.overrides['view_leads'], 'deny')
        self.assertFalse(CrmAccess.objects.get(user=self.target).view_leads)

    def test_unmanaged_role_drift_requires_reconciliation(self):
        service.apply_role_transition(**self.args())
        OrganizationAccess.objects.filter(user=self.target).update(role='installer')
        with self.assertRaises(PermissionDenied):
            service.prepare_role_transition(**self.ids)
        self.assertEqual(CrmRolePolicy.objects.get(user=self.target).roles, ['manager'])

    def test_policy_edit_invalidates_old_confirmation_and_rejects_invalid_metadata(self):
        service.apply_role_transition(**self.args())
        args = self.args(approved=[])
        policy = CrmRolePolicy.objects.get(user=self.target)
        from django.db import connection
        with connection.cursor() as cursor:
            cursor.execute('UPDATE ' + connection.ops.quote_name(CrmRolePolicy._meta.db_table) + ' SET revision=revision+1 WHERE id=%s', [policy.pk])
        policy.refresh_from_db()
        with self.assertRaises(ValidationError): service.apply_role_transition(**args)
        policy.overrides = {'view_leads': 'allow'}
        with self.assertRaises(ValidationError): policy.save()



from unittest import skipUnless
from django.db import connection, close_old_connections
from django.test import TransactionTestCase
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier


@skipUnless(connection.vendor == 'mysql', 'Requires disposable MySQL row locks')
@override_settings(CRM_ROLE_TRANSITION_ENABLED=True)
class RoleTransitionConcurrencyTests(TransactionTestCase):
    setUp = RoleTransitionTests.setUp
    args = RoleTransitionTests.args

    def test_same_snapshot_concurrent_apply_has_one_audit_and_one_rejection(self):
        args = self.args()
        barrier = Barrier(2)
        def apply():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                try:
                    service.apply_role_transition(**args)
                    return 'changed'
                except ValidationError:
                    return 'stale'
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: apply(), range(2)))
        self.assertCountEqual(results, ['changed', 'stale'])
        self.assertEqual(DataAuditLog.objects.filter(entity_type='crm_role_transition_v1').count(), 1)
        self.assertEqual(StaffAccount.objects.get(user=self.target).revision, 1)
