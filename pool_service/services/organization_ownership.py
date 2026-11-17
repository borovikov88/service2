"""Explicit first-owner setup by a protected operator; never an admin fallback."""
import hashlib
import json

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q

from pool_service.communication_models import PhoneCall
from pool_service.models import DataAuditLog, Employee, Organization, OrganizationAccess
from pool_service.staff_call_models import StaffAccount


class OwnershipBlocked(Exception):
    pass


def _positive_id(value):
    return type(value) is int and 0 < value < 2**63


@transaction.atomic
def establish_first_owner(*, organization_id, user_id, expected_sha, run_id,
                          run_attempt, apply=False, expected_plan=None):
    if not _positive_id(organization_id) or not _positive_id(user_id):
        raise OwnershipBlocked('invalid_scope')
    org = Organization.objects.select_for_update().filter(pk=organization_id).first()
    if org is None:
        raise OwnershipBlocked('candidate_unavailable')
    user = get_user_model().objects.select_for_update().filter(pk=user_id).first()
    if user is None or not user.is_active or user.is_superuser:
        raise OwnershipBlocked('candidate_unavailable')
    owners = list(OrganizationAccess.objects.select_for_update().filter(
        organization=org, role='owner').order_by('pk'))
    if any(row.user_id != user_id for row in owners) or len(owners) > 1:
        raise OwnershipBlocked('owner_already_exists')
    roles = list(OrganizationAccess.objects.select_for_update().filter(
        organization=org, user=user).order_by('pk').values_list('pk', 'role'))
    employees = list(Employee.objects.select_for_update().filter(
        organization=org, user=user).order_by('pk'))
    account = StaffAccount.objects.select_for_update().filter(organization=org, user=user).first()
    if (not {'admin', 'owner'}.intersection(role for _, role in roles)
            or len(employees) != 1 or not employees[0].is_active
            or employees[0].employment_status == 'dismissed'
            or account and account.kind in {'service', 'system'}):
        raise OwnershipBlocked('candidate_unavailable')
    employee = employees[0]
    plan = hashlib.sha256(json.dumps([
        expected_sha, org.pk, user.pk, user.username, employee.pk, employee.updated_at.isoformat(),
        account.kind if account else None, roles, [(row.pk, row.user_id) for row in owners],
    ], ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()
    calls = PhoneCall.objects.filter(organization=org, owner_user_snapshot__isnull=True).filter(
        Q(employee_id=user_id) | Q(peer_employee_id=user_id))
    result = dict(schema_version=1, expected_sha=expected_sha, organization_id=org.pk,
        user_id=user.pk, employee_id=employee.pk, mode='apply' if apply else 'preview',
        status='already_owner' if owners else 'ready', plan_digest=plan,
        eligible_calls=calls.count(), roles_added=0, snapshot_count=0, audit_id=None,
        run_id=str(run_id), run_attempt=str(run_attempt))
    if not apply:
        return result
    # Accept either the current preview or a retry of this exact completed
    # operation. A receipt from another deployed version cannot authorize it.
    reviewed = expected_plan == plan
    if not reviewed and owners:
        reviewed = DataAuditLog.objects.filter(organization=org,
            entity_type='OrganizationOwnership', entity_id=str(org.pk),
            after__source='protected_first_owner_workflow', after__user_id=user.pk,
            after__expected_sha=expected_sha, after__plan_digest=expected_plan).exists()
    if not reviewed:
        raise OwnershipBlocked('plan_changed')
    if expected_plan:
        result['plan_digest'] = expected_plan
    grant = owners[0] if owners else OrganizationAccess.objects.create(
        organization=org, user=user, role='owner')
    result['roles_added'] = int(not owners)
    cursor = 0
    while True:
        ids = list(calls.select_for_update().filter(pk__gt=cursor).order_by('pk').values_list('pk', flat=True)[:200])
        if not ids:
            break
        result['snapshot_count'] += calls.filter(pk__in=ids).update(owner_user_snapshot=user.pk)
        cursor = ids[-1]
    if result['roles_added'] or result['snapshot_count']:
        audit = DataAuditLog.objects.create(organization=org, actor=None,
            entity_type='OrganizationOwnership', entity_id=str(org.pk),
            action='create' if result['roles_added'] else 'update',
            before={'roles': [role for _, role in roles]},
            after={'user_id': user.pk, 'owner_role_id': grant.pk,
                'source': 'protected_first_owner_workflow', 'expected_sha': expected_sha,
                'plan_digest': result['plan_digest'], 'run_id': str(run_id),
                'run_attempt': str(run_attempt), 'snapshot_count': result['snapshot_count']},
            changed_fields=['owner_role', 'owner_user_snapshot'])
        result['audit_id'] = audit.pk
    result['status'] = 'applied' if result['roles_added'] else 'already_owner'
    return result
