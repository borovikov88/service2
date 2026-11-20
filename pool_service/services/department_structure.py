"""Dated organization structure, independent from grants in operational modules."""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pool_service.models import Employee, OrganizationAccess
from pool_service.staff_call_models import StaffAccount
from pool_service.staff_structure_models import DepartmentAssignment, StaffDepartment
from .call_processing_settings import _lock_owner


def eligible_employees(organization):
    members = OrganizationAccess.objects.filter(organization=organization).values('user_id')
    people = StaffAccount.objects.filter(organization=organization, kind='person').values('user_id')
    return Employee.objects.filter(organization=organization, user_id__in=members, user__is_active=True,
        user__is_superuser=False, is_active=True).filter(user_id__in=people).exclude(employment_status='dismissed')


@transaction.atomic
def create_department(actor, organization, *, name, parent_id):
    actor = _lock_owner(actor, organization)
    name = ' '.join(name.split())
    if not name or len(name) > 100 or StaffDepartment.objects.filter(organization=organization, name__iexact=name).exists():
        raise ValidationError('Введите уникальное название отдела длиной до 100 символов.')
    parent = None
    if parent_id:
        parent = StaffDepartment.objects.filter(organization=organization, pk=parent_id).first()
        if parent is None:
            raise ValidationError('Родительский отдел недоступен.')
    return StaffDepartment.objects.create(organization=organization, parent=parent, name=name, created_by=actor)


@transaction.atomic
def assign_employee(actor, organization, *, department_id, employee_id, position, includes_children):
    actor = _lock_owner(actor, organization)
    dept = StaffDepartment.objects.filter(organization=organization, pk=department_id).first()
    employee = eligible_employees(organization).select_for_update().filter(pk=employee_id).first()
    if not dept or not employee or position not in dict(DepartmentAssignment.POSITIONS) or includes_children and position != 'sales_head':
        raise ValidationError('Проверьте отдел, сотрудника и должность.')
    active = DepartmentAssignment.objects.filter(valid_to__isnull=True)
    if active.filter(employee=employee).exists():
        raise ValidationError('Сначала завершите текущее назначение сотрудника.')
    if position == 'sales_head' and active.filter(department=dept, position=position).exists():
        raise ValidationError('В отделе уже назначен РОП.')
    return DepartmentAssignment.objects.create(department=dept, employee=employee, position=position,
        includes_children=includes_children, assigned_by=actor, user_at_assignment_id=employee.user_id)


@transaction.atomic
def end_assignment(actor, organization, *, assignment_id, keep_cards):
    actor = _lock_owner(actor, organization)
    if not keep_cards:
        raise ValidationError('Подтвердите, у кого остаются открытые карточки.')
    updated = DepartmentAssignment.objects.filter(pk=assignment_id, department__organization=organization,
        employee__organization=organization, valid_to__isnull=True).update(valid_to=timezone.now(), ended_by=actor)
    if not updated:
        raise ValidationError('Назначение уже изменилось. Обновите страницу.')

def possible_sales_head(call):
    participants = [uid for uid in (call.employee_id, call.peer_employee_id) if uid]
    if not participants or not call.intake_received_at or not call.intake_scope:
        return None
    return DepartmentAssignment.objects.filter(department__organization_id=call.organization_id,
        employee__organization_id=call.organization_id, user_at_assignment_id__in=participants, position='sales_head',
        valid_from__lte=call.intake_received_at).order_by('-valid_from', '-pk').values_list('user_at_assignment_id', flat=True).first()


def sales_head_at(call):
    participants = [uid for uid in (call.employee_id, call.peer_employee_id, call.undated_sales_head) if uid]
    if call.intake_scope and not call.intake_occurred_at:
        return None
    if not participants or not call.started_at or not call.organization_id:
        return None
    assignments = DepartmentAssignment.objects.filter(department__organization_id=call.organization_id,
        employee__organization_id=call.organization_id, user_at_assignment_id__in=participants, position='sales_head',
        valid_from__lte=call.started_at).filter(Q(valid_to__isnull=True) | Q(valid_to__gt=call.started_at))
    return assignments.order_by('user_at_assignment_id').values_list('user_at_assignment_id', flat=True).first()
