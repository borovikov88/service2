"""Staff directory and explicit, organization-scoped lifecycle actions."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.http import require_http_methods
from django.db.models import Q

from .models import Employee, Organization, OrganizationAccess, OrganizationInvite, PoolAccess, User, DataAuditLog
from .communication_models import TelephonyConnection, TelephonyEmployeeIdentity, PhoneCall
from .staff_call_models import StaffAccount, EmployeeTelephoneAssignment, CallWorkContext, CallPrivacyHold
from .services.staff_lifecycle import manages_staff, dismissal_preview, dismissal_recipients, dismiss_employee, assign_phone, close_phone
from .services.call_processing_policy import phone_key


def _response(request, template, context):
    response = render(request, template, {'active_tab': 'users', **context})
    response['Cache-Control'] = 'private, no-store'
    return response


def _date_time(value):
    stamp = parse_datetime(value or '')
    if stamp is None:
        raise ValidationError('Укажите дату и время назначения номера.')
    return timezone.make_aware(stamp) if timezone.is_naive(stamp) else stamp


@login_required
def users_directory(request):
    unavailable = Employee.objects.filter(user=request.user).filter(Q(is_active=False) | Q(employment_status='dismissed')).values('organization_id')
    org_ids = OrganizationAccess.objects.filter(user=request.user, role__in=['owner', 'admin']).exclude(organization_id__in=unavailable).values_list('organization_id', flat=True)
    orgs = Organization.objects.all() if request.user.is_superuser else Organization.objects.filter(pk__in=org_ids)
    if not orgs.exists():
        raise PermissionDenied
    members = list(OrganizationAccess.objects.filter(organization__in=orgs).select_related('user', 'organization').order_by('organization__name', 'user__last_name', 'pk'))
    employees = list(Employee.objects.filter(organization__in=orgs).select_related('user', 'organization'))
    accounts = {(r.organization_id, r.user_id): r for r in StaffAccount.objects.filter(organization__in=orgs)}
    phone_rows = {}
    from .phone_utils import format_phone
    for phone in EmployeeTelephoneAssignment.objects.filter(organization__in=orgs).select_related('connection').order_by('-valid_from'):
        phone.display_number = format_phone(phone.phone_key) if phone.phone_key else ''
        phone_rows.setdefault(phone.employee_id, []).append(phone)
    rows = {}
    for member in members:
        key = (member.organization_id, member.user_id)
        row = rows.setdefault(key, {'user': member.user, 'organization': member.organization, 'roles': [], 'role_codes': [], 'access_id': member.pk, 'employee': None})
        row['roles'].append(member.get_role_display())
        row['role_codes'].append(member.role)
    for employee in employees:
        key = (employee.organization_id, employee.user_id or -employee.pk)
        row = rows.setdefault(key, {'user': employee.user, 'organization': employee.organization, 'roles': []})
        row['employee'] = employee
    for row in rows.values():
        user, employee = row['user'], row.get('employee')
        account = accounts.get((row['organization'].pk, user.pk)) if user else None
        row['name'] = employee.display_name if employee else (user.get_full_name() or user.username)
        row['kind'] = account.get_kind_display() if account else ('Сотрудник' if employee else 'Не проверена')
        row['status'] = employee.get_employment_status_display() if employee else 'Статус сотрудника не задан'
        row['access'] = bool(user and user.is_active and row['roles'] and (not employee or employee.is_active and employee.employment_status != 'dismissed'))
        row['phones'] = phone_rows.get(employee.pk, []) if employee else []
        row['can_manage_roles'] = bool(row.get('access_id') and user and user.pk != request.user.pk
            and 'owner' not in row.get('role_codes', []) and not request.user.is_superuser
            and manages_staff(request.user, row['organization']) and (not employee or employee.is_active))
    return _response(request, 'pool_service/staff_directory.html', {
        'rows': sorted(rows.values(), key=lambda r: (r['organization'].name, r['name'])),
        'invites': OrganizationInvite.objects.filter(organization__in=orgs, accepted_at__isnull=True).select_related('organization'),
        'pool_staff': PoolAccess.objects.filter(pool__organization__in=orgs).select_related('user', 'pool'),
        'can_manage': bool(orgs.exists()) and not request.user.is_superuser,
        'editable_roles': [(value,label) for value,label in OrganizationAccess.ROLE_CHOICES if value != 'owner'],
    })


@login_required
@require_http_methods(['GET', 'POST'])
def account_kind(request, org_id, user_id):
    org = get_object_or_404(Organization, pk=org_id)
    if not manages_staff(request.user, org):
        raise PermissionDenied
    user = get_object_or_404(User.objects.filter(organizationaccess__organization=org).distinct(), pk=user_id)
    account = StaffAccount.objects.filter(organization=org, user=user).first()
    if request.method == 'POST':
        try:
            with transaction.atomic():
                Organization.objects.select_for_update().get(pk=org.pk)
                if not manages_staff(request.user, org):
                    raise PermissionDenied
                user = get_object_or_404(User.objects.select_for_update().filter(
                    organizationaccess__organization=org).distinct(), pk=user_id)
                kind = request.POST.get('kind')
                if kind not in dict(StaffAccount.KINDS):
                    raise ValidationError('Выберите тип учётной записи.')
                if user.pk == request.user.pk or user.is_superuser or OrganizationAccess.objects.filter(organization=org, user=user, role='owner').exists():
                    raise PermissionDenied
                if kind in {'service', 'system'} and Employee.objects.filter(organization=org, user=user).exists():
                    raise ValidationError('Сначала проверьте существующую карточку сотрудника.')
                account, _ = StaffAccount.objects.select_for_update().get_or_create(organization=org, user=user)
                if str(account.revision) != request.POST.get('revision', '0'):
                    raise ValidationError('Запись изменилась. Обновите страницу.')
                before_kind = account.kind
                if kind == 'person':
                    employee = Employee.objects.select_for_update().filter(organization=org, user=user).first()
                    selected = request.POST.get('employee', '').strip()
                    if employee:
                        if selected and selected != str(employee.pk):
                            raise ValidationError('Аккаунт уже связан с другой карточкой сотрудника.')
                    elif selected == 'new':
                        employee = Employee.objects.create(organization=org, user=user,
                            display_name=user.get_full_name() or user.username)
                    else:
                        if not selected.isascii() or not selected.isdigit() or len(selected) > 18:
                            raise ValidationError('Выберите существующую карточку сотрудника или создание новой.')
                        employee = Employee.objects.select_for_update().filter(
                            pk=int(selected), organization=org, user__isnull=True).first()
                        if employee is None:
                            raise ValidationError('Карточка уже связана или недоступна в этой организации.')
                        if user.is_active and (not employee.is_active or employee.employment_status == 'dismissed'):
                            raise ValidationError('Нельзя связать действующий вход с уже уволенным сотрудником.')
                        employee.user = user
                        employee.save(update_fields=['user', 'updated_at'])
                        DataAuditLog.objects.create(organization=org, actor=request.user,
                            entity_type='Employee', entity_id=str(employee.pk), action='update',
                            before={'user_id': None}, after={'user_id': user.pk}, changed_fields=['user_id'])
                account.kind, account.revision = kind, account.revision + 1
                account.save()
                DataAuditLog.objects.create(organization=org, actor=request.user, entity_type='StaffAccount',
                    entity_id=str(account.pk), action='update', before={'kind': before_kind},
                    after={'kind': kind, 'employee_id': employee.pk if kind == 'person' else None}, changed_fields=['kind'])
            return redirect('users')
        except ValidationError as exc:
            messages.error(request, ' '.join(exc.messages))
    linked_employee = Employee.objects.filter(organization=org, user=user).first()
    employee_options = Employee.objects.filter(organization=org).filter(
        Q(user__isnull=True) | Q(user=user)).order_by('display_name', 'pk')
    return _response(request, 'pool_service/staff_account_kind.html', {
        'account': account, 'target': user, 'organization': org, 'kinds': StaffAccount.KINDS,
        'linked_employee': linked_employee, 'employee_options': employee_options})


@login_required
@require_http_methods(['GET', 'POST'])
def employee_control(request, employee_id):
    employee = get_object_or_404(Employee.objects.select_related('organization', 'user'), pk=employee_id)
    if not manages_staff(request.user, employee.organization):
        raise PermissionDenied
    if request.method == 'POST':
        try:
            action = request.POST.get('action')
            if action == 'dismiss':
                raw_date = request.POST.get('dismissed_at', '')
                date = parse_date(raw_date) if raw_date else None
                if raw_date and date is None:
                    raise ValidationError('Проверьте дату увольнения или оставьте её пустой, если она неизвестна.')
                dismiss_employee(actor=request.user, employee_id=employee.pk,
                    expected_updated_at=request.POST.get('revision'), dismissed_at=date,
                    recipient_id=int(request.POST['recipient']) if request.POST.get('recipient') else None)
                messages.success(request, 'Доступ отозван. История сохранена, открытые карточки переназначены.')
                return redirect('users')
            if action == 'phone':
                connection = get_object_or_404(TelephonyConnection, pk=request.POST.get('connection'), organization=employee.organization)
                raw = request.POST.get('phone', '').strip()
                key = phone_key(raw) if raw else ''
                if raw and not key:
                    raise ValidationError('Проверьте внешний номер телефона.')
                assign_phone(actor=request.user, employee=employee, connection=connection, phone_key=key,
                    extension=request.POST.get('extension', '').strip(), provider_user=request.POST.get('provider_user', '').strip(),
                    valid_from=_date_time(request.POST.get('valid_from')),
                    valid_to=_date_time(request.POST['valid_to']) if request.POST.get('valid_to') else None)
                messages.success(request, 'Назначение номера сохранено.')
                return redirect('staff_employee_control', employee_id=employee.pk)
            if action == 'close_phone':
                with transaction.atomic():
                    Organization.objects.select_for_update().get(pk=employee.organization_id)
                    if not manages_staff(request.user, employee.organization):
                        raise PermissionDenied
                    row = get_object_or_404(EmployeeTelephoneAssignment.objects.select_for_update(), pk=request.POST.get('assignment'), employee=employee)
                    close_phone(actor=request.user, assignment_id=row.pk, valid_to=_date_time(request.POST.get('valid_to')))
                return redirect('staff_employee_control', employee_id=employee.pk)
            raise ValidationError('Неизвестное действие.')
        except (ValueError, ValidationError) as exc:
            messages.error(request, ' '.join(exc.messages) if isinstance(exc, ValidationError) else 'Проверьте значения полей.')
    preview = dismissal_preview(request.user, employee)
    return _response(request, 'pool_service/staff_employee_control.html', {
        'employee': employee, 'preview': preview,
        'can_dismiss': employee.user_id != request.user.pk and not (employee.user_id and (
            employee.user.is_superuser or OrganizationAccess.objects.filter(
                organization=employee.organization, user_id=employee.user_id, role='owner').exists())),
        'recipient_users': dismissal_recipients(employee.organization).exclude(pk=employee.user_id),
        'phones': EmployeeTelephoneAssignment.objects.filter(employee=employee).select_related('connection').order_by('-valid_from'),
        'identities': TelephonyEmployeeIdentity.objects.filter(employee=employee).select_related('connection'),
        'connections': TelephonyConnection.objects.filter(organization=employee.organization, is_active=True),
        'today': timezone.localdate(), 'now_local': timezone.localtime().strftime('%Y-%m-%dT%H:%M'),
    })


@login_required
@require_http_methods(['GET', 'POST'])
def call_context(request, call_id):
    from .services.call_processing_settings import settings_allowed
    from .services.call_access import set_work_context
    from .services.call_access import can_read_call
    from .services.crm_lead_access import leads_for_user
    from .services.crm_commercial_access import deals_for_user
    call = get_object_or_404(PhoneCall.objects.select_related('organization'), pk=call_id)
    if not settings_allowed(request.user, call.organization) or not can_read_call(request.user, call) or call.direction == 'internal':
        raise PermissionDenied
    context = CallWorkContext.objects.filter(call=call).first()
    if request.method == 'POST':
        try:
            kind, pk = request.POST.get('subject', '').split(':')
            if kind not in {'lead', 'deal'}:
                raise ValueError
            set_work_context(actor=request.user, call_id=call.pk, revision=int(request.POST.get('revision', 0)),
                **{kind + '_id': int(pk)})
            messages.success(request, 'Основная карточка разговора сохранена.')
            return redirect('communication_call_context', call_id=call.pk)
        except (ValueError, ValidationError) as exc:
            messages.error(request, ' '.join(exc.messages) if isinstance(exc, ValidationError) else 'Выберите одну карточку.')
    listeners = None
    if request.method == "GET" and request.GET.get("access") == "1":
        from .services.call_access import can_access_call_content
        members = User.objects.filter(is_active=True, organizationaccess__organization=call.organization,
            communication_accesses__organization=call.organization, communication_accesses__can_listen_calls=True
        ).distinct().order_by("last_name", "first_name", "pk")
        listeners = [member.get_full_name() or member.username for member in members
            if can_access_call_content(member, call)]
    return _response(request, 'pool_service/call_work_context.html', {'call': call, 'context': context, 'listeners': listeners,
        'leads': leads_for_user(request.user, call.organization).filter(is_archived=False).order_by('-pk'),
        'deals': deals_for_user(request.user, call.organization).filter(is_archived=False).order_by('-pk')})


@login_required
@require_http_methods(['GET', 'POST'])
def personal_history(request):
    from .services.call_processing_settings import _lock_owner, settings_allowed
    from .services.call_privacy import is_private_call
    from .communication_views import _selected_access
    access = _selected_access(request)
    if not access or not settings_allowed(request.user, access.organization):
        raise PermissionDenied
    org = access.organization
    if request.method == 'POST':
        with transaction.atomic():
            _lock_owner(request.user, org)
            hold = get_object_or_404(CallPrivacyHold.objects.select_for_update(), pk=request.POST.get('hold'),
                organization=org, call__organization=org, owner=request.user, released_at__isnull=True)
            # The active private-number rule remains authoritative after release.
            hold.released_at = timezone.now()
            hold.save(update_fields=['released_at'])
            DataAuditLog.objects.create(organization=org, actor=request.user, entity_type='CallPrivacyHold',
                entity_id=str(hold.pk), action='restore', after={'call_id': hold.call_id}, changed_fields=['released_at'])
        from django.urls import reverse
        return redirect(reverse('communication_personal_history') + '?org_id=' + str(org.pk))
    return _response(request, 'pool_service/personal_call_history.html', {'organization': org, 'holds': CallPrivacyHold.objects.filter(
        organization=org, call__organization=org, owner=request.user, released_at__isnull=True).select_related('call').order_by('-created_at')})


@login_required
def legacy_staff_action(request, access_id):
    access = get_object_or_404(OrganizationAccess, pk=access_id)
    if not manages_staff(request.user, access.organization):
        raise PermissionDenied
    employee = Employee.objects.filter(organization=access.organization, user=access.user).first()
    if employee:
        return redirect('staff_employee_control', employee_id=employee.pk)
    return redirect('staff_account_kind', org_id=access.organization_id, user_id=access.user_id)
