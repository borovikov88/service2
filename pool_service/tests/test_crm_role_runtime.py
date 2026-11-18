from django.test import TestCase, override_settings
from django.core.exceptions import ValidationError
from django.db import connection
from pool_service.tests.test_crm_role_transition import RoleTransitionTests
from pool_service.models import OrganizationAccess
from pool_service.crm_commercial_models import CrmAccess
from pool_service.crm_role_models import CrmRolePolicy
from pool_service.communication_models import CommunicationAccess
from pool_service.services import crm_role_transition as service
from pool_service.services.crm_role_runtime import resolve_crm_capabilities
from pool_service.services.crm_role_policy_preview import CAPABILITIES
from pool_service.services.crm_commercial_access import can_view_team_deals
from pool_service.services.crm_lead_access import can_view_leads


@override_settings(CRM_ROLE_TRANSITION_ENABLED=True)
class RuntimeRoleTests(TestCase):
    setUp = RoleTransitionTests.setUp
    args = RoleTransitionTests.args

    def test_unmanaged_legacy_implicit_team_read_remains_distinct_from_flag(self):
        OrganizationAccess.objects.filter(user=self.target).update(role='manager')
        result = resolve_crm_capabilities(self.target.pk, self.org.pk)
        self.assertFalse(result.managed); self.assertTrue(result.team_read)
        self.assertFalse(result.view_team_deals); self.assertFalse(result.view_leads)

    def test_managed_role_drift_closes_implicit_manager_and_lead_read(self):
        service.apply_role_transition(**self.args())
        self.assertTrue(can_view_team_deals(self.target, self.org))
        OrganizationAccess.objects.filter(user=self.target).update(role='installer')
        result = resolve_crm_capabilities(self.target.pk, self.org.pk)
        self.assertFalse(result.valid); self.assertFalse(any(result.flags.values()))
        self.assertFalse(can_view_team_deals(self.target, self.org))
        self.assertFalse(can_view_leads(self.target, self.org))

    def test_out_of_band_raw_grant_cannot_override_reviewed_deny(self):
        overrides = dict.fromkeys(CAPABILITIES, 'inherit'); overrides['view_leads'] = 'deny'
        service.apply_role_transition(**self.args(overrides=overrides, approved=[f for f in CAPABILITIES if f != 'view_leads']))
        grant = CrmAccess.objects.get(user=self.target)
        grant.view_leads = True
        with self.assertRaises(ValidationError): grant.save()
        # Unsupported direct SQL is simulated only in disposable tests. Runtime
        # must remain restrictive even if a privileged writer bypassed model save.
        with connection.cursor() as cursor:
            cursor.execute('UPDATE ' + connection.ops.quote_name(CrmAccess._meta.db_table) + ' SET view_leads=%s WHERE id=%s', [True, grant.pk])
        self.assertFalse(resolve_crm_capabilities(self.target.pk, self.org.pk).view_leads)
        self.assertFalse(can_view_leads(self.target, self.org))

    def test_revoke_is_allowed_but_legacy_regrant_is_not(self):
        service.apply_role_transition(**self.args())
        grant = CrmAccess.objects.get(user=self.target); grant.view_leads = False; grant.save()
        self.assertFalse(resolve_crm_capabilities(self.target.pk, self.org.pk).view_leads)
        grant.view_leads = True
        with self.assertRaises(ValidationError): grant.save()

    def test_policy_cannot_be_removed_or_rewritten_by_unreviewed_writer(self):
        service.apply_role_transition(**self.args())
        policy = CrmRolePolicy.objects.get(user=self.target)
        with self.assertRaises(ValidationError): policy.save()
        with self.assertRaises(ValidationError): policy.delete()
        with self.assertRaises(ValidationError): CrmRolePolicy.objects.filter(pk=policy.pk).delete()

    def test_role_signals_do_not_restore_communication_denies(self):
        service.apply_role_transition(**self.args())
        grant = CommunicationAccess.objects.get(user=self.target)
        self.assertFalse(grant.can_listen_calls)
        added = OrganizationAccess.objects.create(organization=self.org, user=self.target, role='installer')
        grant.refresh_from_db(); self.assertFalse(grant.can_listen_calls)
        added.delete()
        grant.refresh_from_db(); self.assertFalse(grant.can_listen_calls)

    def test_identity_resolution_flag_is_not_granted_by_crm_policy(self):
        service.apply_role_transition(**self.args())
        self.assertFalse(CrmAccess.objects.get(user=self.target).resolve_contact_identity)

    def test_drift_deny_to_allow_requires_effective_read_confirmation(self):
        overrides = dict.fromkeys(CAPABILITIES, 'inherit'); overrides['view_leads'] = 'deny'
        service.apply_role_transition(**self.args(overrides=overrides, approved=[f for f in CAPABILITIES if f != 'view_leads']))
        grant = CrmAccess.objects.get(user=self.target)
        with connection.cursor() as cursor:
            cursor.execute('UPDATE ' + connection.ops.quote_name(CrmAccess._meta.db_table) + ' SET view_leads=%s WHERE id=%s', [True, grant.pk])
        ids = dict(actor_id=self.owner.pk, organization_id=self.org.pk, target_id=self.target.pk)
        prepared = service.prepare_role_transition(**ids)
        self.assertTrue(prepared['snapshot']['flags']['view_leads'])
        self.assertFalse(prepared['snapshot']['effective_flags']['view_leads'])
        overrides['view_leads'] = 'allow'
        args = dict(**ids, proposed_roles=['manager'], overrides=overrides,
            approved_increases=['view_leads'], expected_digest=prepared['expected_digest'])
        with self.assertRaises(ValidationError):
            service.apply_role_transition(**args)
        self.assertFalse(can_view_leads(self.target, self.org))
        self.assertTrue(service.apply_role_transition(**args, acknowledge_assigned_call_visibility=True)['changed'])
        self.assertTrue(can_view_leads(self.target, self.org))
