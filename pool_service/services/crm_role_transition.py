"""Opt-in private role transition prototype; not wired to UI or production.

Trusted callers must authenticate the owner independently. Every legacy CRM
capability decision is explicit; this is not a bulk grant or automatic migration.
"""
import hashlib
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from pool_service.models import Organization, OrganizationAccess, Employee, DataAuditLog
from pool_service.staff_call_models import StaffAccount
from pool_service.crm_commercial_models import CrmAccess
from pool_service.crm_role_models import CrmRolePolicy
from pool_service.communication_models import CommunicationAccess, _communication_access_defaults
from .crm_role_policy_preview import CAPABILITIES, preview_role_change

ERROR = 'crm_role_transition_unavailable'
SAFE_ROLES = frozenset(('manager', 'service', 'installer'))
COMM_FIELDS = tuple(_communication_access_defaults(None))


def _guard_ids(actor_id, organization_id, target_id):
    if any(type(v) is not int or not 0 < v < 2**63 for v in (actor_id, organization_id, target_id)) or actor_id == target_id:
        raise PermissionDenied(ERROR)


def _locked_state(actor_id, organization_id, target_id):
    _guard_ids(actor_id, organization_id, target_id)
    org = Organization.objects.select_for_update().filter(pk=organization_id).first()
    if org is None:
        raise PermissionDenied(ERROR)
    users = {u.pk: u for u in get_user_model().objects.select_for_update().filter(
        pk__in=sorted((actor_id, target_id))).order_by('pk').only('pk', 'is_active', 'is_superuser')}
    actor, target = users.get(actor_id), users.get(target_id)
    if not actor or not target or any(not u.is_active or u.is_superuser for u in (actor, target)):
        raise PermissionDenied(ERROR)
    accesses = list(OrganizationAccess.objects.select_for_update().filter(
        organization=org, user_id__in=(actor_id, target_id)).order_by('pk'))
    if not any(a.user_id == actor_id and a.role == 'owner' for a in accesses):
        raise PermissionDenied(ERROR)
    target_roles = sorted({a.role for a in accesses if a.user_id == target_id})
    if not target_roles or not set(target_roles) <= SAFE_ROLES:
        raise PermissionDenied(ERROR)
    employees = list(Employee.objects.select_for_update().filter(
        organization=org, user_id__in=(actor_id, target_id)).only('pk', 'is_active', 'employment_status'))
    if any(not e.is_active or e.employment_status == Employee.STATUS_DISMISSED for e in employees):
        raise PermissionDenied(ERROR)
    account = StaffAccount.objects.select_for_update().filter(organization=org, user=target).first()
    if not account or account.kind != 'person':
        raise PermissionDenied(ERROR)
    grant = CrmAccess.objects.select_for_update().filter(organization=org, user=target).first()
    comm = CommunicationAccess.objects.select_for_update().filter(organization=org, user=target).first()
    policy = CrmRolePolicy.objects.select_for_update().filter(organization=org, user=target).first()
    if policy:
        try:
            policy.clean()
        except ValidationError:
            raise PermissionDenied(ERROR) from None
        if policy.roles != target_roles:
            raise PermissionDenied(ERROR)
    state = {'schema': 1,
        'policy_revision': policy.revision if policy else None,
        'policy_overrides': policy.overrides if policy else None, 'organization_id': org.pk, 'target_id': target.pk,
        'roles': target_roles, 'account_revision': account.revision,
        'crm_read': org.crm_v2_read, 'crm_write': org.crm_v2_write,
        'grant_present': grant is not None,
        'flags': {f: bool(grant and getattr(grant, f)) for f in CAPABILITIES},
        'communication_present': comm is not None,
        'communication': {f: bool(comm and getattr(comm, f)) for f in COMM_FIELDS}}
    # Approval must reflect effective access, not potentially stale raw flags.
    state['effective_flags'] = dict(state['flags'])
    if policy:
        from .crm_role_runtime import resolve_crm_capabilities
        effective = resolve_crm_capabilities(target.pk, org.pk, lock=True)
        if not effective.valid:
            raise PermissionDenied(ERROR)
        state['effective_flags'] = dict(effective.flags)
    return org, actor, target, account, state


def _digest(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@transaction.atomic
def prepare_role_transition(*, actor_id, organization_id, target_id):
    """Owner-only bounded state; hash is optimistic concurrency, NOT authority."""
    *_, state = _locked_state(actor_id, organization_id, target_id)
    return {'snapshot': state, 'expected_digest': _digest(state)}


@transaction.atomic
def apply_role_transition(*, actor_id, organization_id, target_id, proposed_roles,
                          overrides, approved_increases, expected_digest,
                          acknowledge_assigned_call_visibility=False):
    """Local prototype; disabled unless explicitly enabled by deployment design.

    A full independently reviewed inherit/allow/deny map is mandatory. Existing
    false flags are not silently upgraded; exact new grants require confirmation.
    Communication flags can only stay unchanged or decrease. Caller-supplied
    digest/approval never replaces current owner and lifecycle authorization.
    """
    if getattr(settings, 'CRM_ROLE_TRANSITION_ENABLED', False) is not True:
        raise PermissionDenied(ERROR)
    if type(proposed_roles) not in (tuple, list) or not proposed_roles or any(
            type(role) is not str or role not in SAFE_ROLES for role in proposed_roles):
        raise ValidationError(ERROR)
    if type(overrides) is not dict or set(overrides) != set(CAPABILITIES):
        raise ValidationError(ERROR)
    if type(approved_increases) not in (tuple, list) or any(
            type(f) is not str or f not in CAPABILITIES for f in approved_increases):
        raise ValidationError(ERROR)
    org, actor, target, account, before = _locked_state(actor_id, organization_id, target_id)
    if type(expected_digest) is not str or expected_digest != _digest(before):
        raise ValidationError(ERROR)
    try:
        proposal = preview_role_change(proposed_roles=proposed_roles,
            current_flags=before['effective_flags'], overrides=overrides, active=True,
            dismissed=False, member=True, crm_read=org.crm_v2_read,
            crm_write=org.crm_v2_write, account_kind='person', provenance='managed')
    except ValueError:
        raise ValidationError(ERROR) from None
    flags = {row.capability: row.proposed for row in proposal.rows}
    # CRM read grants may also expose assigned call sources through the existing
    # object policy even when raw communication flags do not increase.
    if set(proposal.increases) & {'view_leads', 'view_team_deals'} and acknowledge_assigned_call_visibility is not True:
        raise ValidationError(ERROR)
    if set(approved_increases) != set(proposal.increases):
        raise ValidationError(ERROR)
    roles = sorted(set(proposed_roles))
    defaults = _communication_access_defaults('manager' if 'manager' in roles else None)
    communication = {f: before['communication'][f] and defaults[f] for f in COMM_FIELDS}
    if (roles == before['roles'] and flags == before['flags'] and communication == before['communication']
            and before['policy_overrides'] == overrides):
        return {'changed': False, 'revision': account.revision}
    from .crm_role_runtime import reviewed_write
    with reviewed_write(org.pk, target.pk):
        # Existing signal writers run inside this transaction; final capabilities
        # are reduced to the old restrictions intersected with new role defaults.
        OrganizationAccess.objects.filter(organization=org, user=target).exclude(role__in=roles).delete()
        for role in roles:
            OrganizationAccess.objects.get_or_create(organization=org, user=target, role=role)
        CrmAccess.objects.update_or_create(organization=org, user=target, defaults=flags)
        CommunicationAccess.objects.update_or_create(organization=org, user=target, defaults=communication)
        policy, _ = CrmRolePolicy.objects.update_or_create(organization=org, user=target, defaults={
            'roles': roles, 'overrides': dict(overrides), 'reviewed_by': actor,
            'revision': (before['policy_revision'] or 0) + 1, 'version': 1})
        account.revision += 1
        account.save(update_fields=['revision'])
        after = {**before, 'policy_revision': policy.revision, 'policy_overrides': dict(overrides), 'roles': roles, 'flags': flags, 'effective_flags': flags, 'grant_present': True,
            'communication': communication, 'communication_present': True,
            'account_revision': account.revision}
        event = DataAuditLog.objects.create(organization=org, actor=actor,
            entity_type='crm_role_transition_v1', entity_id=str(target.pk), action='update',
            before=before, after={**after, 'reviewed_overrides': overrides,
                'approved_increases': sorted(set(approved_increases)),
                'assigned_call_visibility_acknowledged': acknowledge_assigned_call_visibility is True},
            changed_fields=['roles', 'crm_flags', 'communication', 'account_revision', 'policy_revision', 'policy_overrides'])
        return {'changed': True, 'revision': account.revision, 'audit_id': event.pk}
