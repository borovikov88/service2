"""Pure, shadow-only CRM role preview. NOT an authorization or mutation API.

Callers must supply a private, scoped snapshot. No database/network/model imports,
role changes, migrations, signals or runtime policy integration are performed.
"""
from dataclasses import dataclass

VERSION = 'crm-role-preview-v1'
ROLES = frozenset(('owner', 'admin', 'manager', 'accountant', 'service', 'installer'))
CAPABILITIES = (
    'view_team_deals', 'view_leads', 'create_own_leads', 'take_leads',
    'qualify_own_leads', 'edit_own_leads', 'transition_own_leads',
    'edit_own_deals', 'transition_own_deals', 'create_own_deals',
    'manage_own_next_action',
)
READ_CAPABILITIES = frozenset(('view_team_deals', 'view_leads'))
COMMERCIAL_ROLES = frozenset(('owner', 'admin', 'manager'))


@dataclass(frozen=True)
class CapabilityPreview:
    capability: str
    before: bool
    proposed: bool
    reason: str


@dataclass(frozen=True)
class RolePreview:
    version: str
    rows: tuple[CapabilityPreview, ...]
    review_reasons: tuple[str, ...]
    # Deliberately never authorizes a write, including no-difference snapshots.
    authorizes_change: bool = False

    @property
    def increases(self):
        return tuple(row.capability for row in self.rows if not row.before and row.proposed)

    @property
    def decreases(self):
        return tuple(row.capability for row in self.rows if row.before and not row.proposed)


def preview_role_change(*, proposed_roles, current_flags, overrides=None,
                        active, dismissed, member, crm_read, crm_write,
                        account_kind, provenance):
    """Preview inherited CRM flags only; private call/object policy is not modeled.

    `current_flags` must explicitly contain all supported capabilities, so missing
    legacy state cannot be silently treated as False. `overrides` contains only
    independently verified explicit decisions; legacy True/False is NOT one.
    Nonhuman/unclassified accounts require a separate scope design, not a human
    role template. Their result contains no proposed capability rows.
    """
    if type(proposed_roles) not in (tuple, list, set, frozenset):
        raise ValueError('invalid_roles')
    if any(type(role) is not str or role not in ROLES for role in proposed_roles):
        raise ValueError('invalid_roles')
    roles = frozenset(proposed_roles)
    if type(current_flags) is not dict or set(current_flags) != set(CAPABILITIES):
        raise ValueError('incomplete_current_flags')
    if any(type(value) is not bool for value in current_flags.values()):
        raise ValueError('invalid_current_flags')
    overrides = {} if overrides is None else overrides
    if type(overrides) is not dict or not set(overrides) <= set(CAPABILITIES):
        raise ValueError('invalid_overrides')
    if any(type(value) is not str or value not in ('inherit', 'allow', 'deny')
           for value in overrides.values()):
        raise ValueError('invalid_overrides')
    if any(type(value) is not bool for value in (active, dismissed, member, crm_read, crm_write)):
        raise ValueError('invalid_guards')
    if type(account_kind) is not str or account_kind not in ('person', 'service', 'system', 'unknown'):
        raise ValueError('invalid_account_kind')
    if type(provenance) is not str or provenance not in ('managed', 'legacy_unverified'):
        raise ValueError('invalid_provenance')
    reasons = ['preview_only', 'object_and_communication_policy_not_evaluated']
    if provenance == 'legacy_unverified':
        reasons.append('legacy_grants_require_review_not_automatic_override')
    if account_kind != 'person':
        return RolePreview(VERSION, (), tuple(reasons + ['account_kind_requires_separate_scope']))
    inherited = bool(roles & COMMERCIAL_ROLES)
    rows = []
    for capability in CAPABILITIES:
        override = overrides.get(capability, 'inherit')
        value = inherited if override == 'inherit' else override == 'allow'
        reason = 'role_default' if override == 'inherit' else 'explicit_' + override
        # Restrictive guards win over any base or explicit allow.
        if not active:
            value, reason = False, 'inactive'
        elif dismissed:
            value, reason = False, 'dismissed'
        elif not member or not roles:
            value, reason = False, 'no_membership'
        elif not crm_read:
            value, reason = False, 'crm_read_disabled'
        elif capability not in READ_CAPABILITIES and not crm_write:
            value, reason = False, 'crm_write_disabled'
        rows.append(CapabilityPreview(capability, current_flags[capability], value, reason))
    return RolePreview(VERSION, tuple(rows), tuple(reasons))
