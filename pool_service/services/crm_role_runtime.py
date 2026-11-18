"""Restrictive shared CRM resolver. Never promotes persisted raw grants.

Legacy users retain their existing policy. Managed members require matching
reviewed roles and a verified active human identity; raw grants are intersected
with their reviewed role/override policy. Object/call privacy remains separate.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from .crm_role_policy_preview import CAPABILITIES, preview_role_change

_write_scope = ContextVar('crm_role_write_scope', default=None)


@contextmanager
def reviewed_write(organization_id, user_id):
    token = _write_scope.set((organization_id, user_id))
    try:
        yield
    finally:
        _write_scope.reset(token)


def is_reviewed_write(organization_id, user_id):
    return _write_scope.get() == (organization_id, user_id)


@dataclass(frozen=True)
class RuntimeCapabilities:
    flags: object
    managed: bool
    valid: bool
    team_read: bool
    present: bool = True

    def __bool__(self):
        return self.present and self.valid

    def __getattr__(self, name):
        if name in CAPABILITIES:
            return self.flags[name]
        raise AttributeError(name)


def resolve_crm_capabilities(user_id, organization_id, *, using='default', lock=False):
    from pool_service.models import Organization, OrganizationAccess, Employee
    from pool_service.crm_commercial_models import CrmAccess
    from pool_service.crm_role_models import CrmRolePolicy
    from pool_service.staff_call_models import StaffAccount
    if lock and not transaction.get_connection(using).in_atomic_block:
        raise ValueError('Locking resolver requires an existing organization transaction.')
    def current(model):
        qs = model.objects.using(using)
        return qs.select_for_update() if lock else qs
    if lock:
        # Acquire the same mutex/order even if a future caller only opened atomic.
        org = current(Organization).filter(pk=organization_id).only('pk', 'crm_v2_read', 'crm_v2_write').first()
        user = current(get_user_model()).filter(pk=user_id).only('pk', 'is_active', 'is_superuser').first()
        list(current(OrganizationAccess).filter(user_id=user_id, organization_id=organization_id).order_by('pk'))
        list(current(Employee).filter(user_id=user_id, organization_id=organization_id).order_by('pk'))
        account = current(StaffAccount).filter(user_id=user_id, organization_id=organization_id).first()
    grant = current(CrmAccess).filter(user_id=user_id, organization_id=organization_id).first()
    raw = {f: bool(grant and getattr(grant, f)) for f in CAPABILITIES}
    roles = sorted(set(current(OrganizationAccess).filter(user_id=user_id,
        organization_id=organization_id).values_list('role', flat=True)))
    policy = current(CrmRolePolicy).filter(user_id=user_id, organization_id=organization_id).first()
    if policy is None:
        return RuntimeCapabilities(MappingProxyType(raw), False, True, bool('manager' in roles or raw['view_team_deals']), bool(grant))
    denied = RuntimeCapabilities(MappingProxyType(dict.fromkeys(CAPABILITIES, False)), True, False, False, bool(grant))
    if not lock:
        user = current(get_user_model()).filter(pk=user_id).only('pk', 'is_active', 'is_superuser').first()
        org = current(Organization).filter(pk=organization_id).only('pk', 'crm_v2_read', 'crm_v2_write').first()
        account = current(StaffAccount).filter(user_id=user_id, organization_id=organization_id).first()
    try:
        policy.clean()
        if not user or not org or user.is_superuser or not account or account.kind != 'person' or roles != policy.roles:
            return denied
        proposal = preview_role_change(proposed_roles=roles, current_flags=raw,
            overrides=policy.overrides, active=user.is_active,
            dismissed=current(Employee).filter(user_id=user_id, organization_id=organization_id).filter(
                Q(is_active=False) | Q(employment_status='dismissed')).exists(),
            member=bool(roles), crm_read=org.crm_v2_read, crm_write=org.crm_v2_write,
            account_kind=account.kind, provenance='managed')
    except (ValidationError, ValueError):
        return denied
    flags = {r.capability: raw[r.capability] and r.proposed for r in proposal.rows}
    return RuntimeCapabilities(MappingProxyType(flags), True, True, flags['view_team_deals'], bool(grant))


def guard_managed_grant_save(grant):
    """Legacy provisioners may revoke, but never restore reviewed or revoked rights."""
    from pool_service.crm_role_models import CrmRolePolicy
    if is_reviewed_write(grant.organization_id, grant.user_id):
        return
    using = grant._state.db or 'default'
    if not CrmRolePolicy.objects.using(using).filter(organization_id=grant.organization_id, user_id=grant.user_id).exists():
        return
    old = type(grant).objects.using(using).filter(pk=grant.pk).first() if grant.pk else None
    if old is None or any(not getattr(old, f) and getattr(grant, f) for f in CAPABILITIES):
        raise ValidationError('Reviewed CRM grants require the role confirmation service.')
