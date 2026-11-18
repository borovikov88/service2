"""Private operator adapter for a role preview. No writes or runtime integration.

Actor ID must be independently verified by the trusted caller. This module does
not establish a login session and must not be exposed as an unguarded endpoint.
"""
from dataclasses import asdict

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db.models import Q

from pool_service.crm_commercial_models import CrmAccess
from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.staff_call_models import StaffAccount
from pool_service.services.staff_lifecycle import manages_staff
from pool_service.services.crm_role_policy_preview import CAPABILITIES, ROLES, preview_role_change

UNAVAILABLE = 'crm_role_preview_unavailable'


def _read_snapshot(actor_id, organization_id, target_id):
    org = Organization.objects.only('pk', 'crm_v2_read', 'crm_v2_write').filter(pk=organization_id).first()
    actor = get_user_model().objects.only('pk', 'is_active', 'is_superuser').filter(pk=actor_id).first()
    if not org or not manages_staff(actor, org) or actor_id == target_id:
        raise PermissionDenied(UNAVAILABLE)
    target = get_user_model().objects.only('pk', 'is_active', 'is_superuser').filter(pk=target_id, is_superuser=False).first()
    roles = tuple(sorted(set(OrganizationAccess.objects.filter(organization=org,
        user_id=target_id).values_list('role', flat=True))))
    # Match the existing user-role editor: no owner changes or implicit membership creation.
    if not target or not roles or 'owner' in roles or not set(roles) <= ROLES:
        raise PermissionDenied(UNAVAILABLE)
    grant = CrmAccess.objects.only('pk', *CAPABILITIES).filter(organization=org, user=target).first()
    account = StaffAccount.objects.only('kind', 'revision').filter(organization=org, user=target).first()
    return {
        'active': target.is_active,
        'dismissed': Employee.objects.filter(organization=org, user=target).filter(
            Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED)).exists(),
        'member': True,
        'crm_read': org.crm_v2_read,
        'crm_write': org.crm_v2_write,
        'account_kind': account.kind if account else 'unknown',
        'account_revision': account.revision if account else None,
        'current_roles': roles,
        'current_flags': {field: bool(grant and getattr(grant, field)) for field in CAPABILITIES},
        'grant_present': grant is not None,
    }


def preview_member_crm_roles(*, actor_id, organization_id, target_id, proposed_roles):
    """Read twice to reject changed snapshots; never a token authorizing later writes.

    Existing boolean grants have no trustworthy per-field provenance. Do not
    fabricate overrides from them. Unclassified accounts receive no automated
    human-role proposal. Names, contacts, call data and CRM contents are not read.
    """
    try:
        if any(type(value) is not int or not 0 < value < 2**63
               for value in (actor_id, organization_id, target_id)):
            raise PermissionDenied(UNAVAILABLE)
        if type(proposed_roles) not in (list, tuple) or not proposed_roles:
            raise PermissionDenied(UNAVAILABLE)
        if any(type(role) is not str or role not in ROLES or role == 'owner' for role in proposed_roles):
            raise PermissionDenied(UNAVAILABLE)
        snapshot = _read_snapshot(actor_id, organization_id, target_id)
        proposal = preview_role_change(proposed_roles=proposed_roles, provenance='legacy_unverified',
            **{key: snapshot[key] for key in ('active', 'dismissed', 'member', 'crm_read', 'crm_write',
                'account_kind', 'current_flags')})
        if snapshot != _read_snapshot(actor_id, organization_id, target_id):
            raise PermissionDenied(UNAVAILABLE)
        return {'schema_version': 1, 'organization_id': organization_id, 'target_id': target_id,
            'current_roles': snapshot['current_roles'], 'proposed_roles': tuple(sorted(set(proposed_roles))),
            'grant_present': snapshot['grant_present'], 'preview': asdict(proposal),
            'increases': proposal.increases, 'decreases': proposal.decreases,
            'limitations': ('not_an_atomic_snapshot', 'no_override_provenance',
                'no_communication_or_financial_changes', 'no_authorization_to_apply')}
    except Exception:
        raise PermissionDenied(UNAVAILABLE) from None
