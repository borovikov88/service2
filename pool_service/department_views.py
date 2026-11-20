from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from pool_service.models import Organization
from pool_service.services.call_processing_settings import settings_allowed
from pool_service.services.department_structure import assign_employee, create_department, eligible_employees, end_assignment
from pool_service.staff_structure_models import DepartmentAssignment, StaffDepartment


def _id(data, name, *, optional=False):
    raw = data.get(name, '')
    if optional and raw == '':
        return None
    if not raw.isascii() or not raw.isdecimal() or len(raw) > 18:
        raise ValidationError('Обновите страницу и выберите значение из списка.')
    return int(raw)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def departments(request, organization_id):
    org = get_object_or_404(Organization, pk=organization_id)
    if not settings_allowed(request.user, org):
        raise PermissionDenied
    error = None
    if request.method == 'POST':
        data = request.POST
        try:
            if data.get('action') == 'create':
                create_department(request.user, org, name=data.get('name', ''), parent_id=_id(data, 'parent', optional=True))
            elif data.get('action') == 'assign':
                assign_employee(request.user, org, department_id=_id(data, 'department'), employee_id=_id(data, 'employee'),
                    position=data.get('position'), includes_children=data.get('children') == '1')
            elif data.get('action') == 'end':
                end_assignment(request.user, org, assignment_id=_id(data, 'assignment'), keep_cards=data.get('keep_cards') == '1')
            else:
                raise ValidationError('Обновите страницу и повторите действие.')
        except ValidationError as exc:
            error = ' '.join(exc.messages)
        else:
            return redirect('staff_departments', organization_id=org.pk)
    units = list(StaffDepartment.objects.filter(organization=org).select_related('parent').order_by('name'))
    history = DepartmentAssignment.objects.filter(department__organization=org, employee__organization=org).select_related(
        'employee', 'department').order_by('-valid_from', '-pk')
    active = list(history.filter(valid_to__isnull=True))
    heads = {a.department_id: a.employee for a in active if a.position == 'sales_head'}
    for unit in units:
        unit.head = heads.get(unit.pk)
    return render(request, 'pool_service/departments.html', {'active_tab': 'users', 'org': org, 'units': units,
        'people': eligible_employees(org).exclude(pk__in=[a.employee_id for a in active]).order_by('display_name'),
        'positions': DepartmentAssignment.POSITIONS, 'history': Paginator(history, 35).get_page(request.GET.get('page')),
        'error': error}, status=409 if error else 200)
