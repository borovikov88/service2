"""As-of phone ownership. New assignments never rewrite old calls."""
from django.db.models import Q
from pool_service.models import OrganizationAccess
from pool_service.staff_call_models import EmployeeTelephoneAssignment
from .call_processing_policy import phone_key


def assignment_at(call, *, peer=False):
    if not call.started_at or not call.connection_id:
        return None
    extension = call.peer_provider_extension if peer else call.provider_extension
    provider = call.peer_provider_user if peer else call.provider_user
    key = phone_key(call.phone_number) if peer and call.direction != 'internal' else ''
    query = Q(pk__in=[])
    for field, value in [('extension', extension), ('provider_user', provider), ('phone_key', key)]:
        if value:
            query |= Q(**{field: value})
    rows = list(EmployeeTelephoneAssignment.objects.filter(query, organization_id=call.organization_id,
        connection_id=call.connection_id, valid_from__lte=call.started_at).filter(
        Q(valid_to__isnull=True) | Q(valid_to__gt=call.started_at)).filter(
        Q(revoked_at__isnull=True) | Q(revoked_at__gt=call.started_at)).select_related('employee')[:3])
    if len(rows) != 1 or rows[0].shared or not rows[0].employee_id:
        return None
    row = rows[0]
    from django.utils.dateparse import parse_datetime
    stamp = parse_datetime(call.started_at) if isinstance(call.started_at, str) else call.started_at
    from django.utils import timezone
    if row.employee.dismissed_at and timezone.localtime(stamp).date() > row.employee.dismissed_at:
        return None
    # Conflicting provider identifiers are not resolved by choosing one field.
    if extension and row.extension and row.extension != extension or provider and row.provider_user and row.provider_user != provider:
        return None
    return row


def initialize_call_identity(call):
    first, second = assignment_at(call), assignment_at(call, peer=True)
    if first:
        call.employee_profile_id, call.employee_id = first.employee_id, first.employee.user_id
    if first and second:
        call.employee_profile_id, call.employee_id = first.employee_id, first.employee.user_id
        call.peer_employee_profile_id, call.peer_employee_id = second.employee_id, second.employee.user_id
        call.direction = 'internal'
        call.client_id = None
        call.classification_evidence = {'kind': 'internal', 'assignments': [first.pk, second.pk]}
    elif call.direction == 'internal':
        # Provider-internal history is retained. Unresolved participants stay restricted.
        call.classification_evidence = {'kind': 'internal' if first and second else 'review', 'source': 'provider'}
    owners = set(OrganizationAccess.objects.filter(organization_id=call.organization_id,
        role='owner', user_id__in=[call.employee_id, call.peer_employee_id]).values_list('user_id', flat=True))
    if owners:
        call.owner_user_snapshot = min(owners)
