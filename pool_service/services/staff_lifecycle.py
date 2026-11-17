"""Explicit offboarding, preserving actors and source records."""
from django.contrib.auth import get_user_model
from datetime import datetime, time, timedelta
from django.contrib.sessions.models import Session
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pool_service.models import (ClientAccess, DataAuditLog, Employee, Organization,
                                 OrganizationAccess, PoolAccess, Client)
from pool_service.staff_call_models import StaffAccount, EmployeeTelephoneAssignment


def manages_staff(actor, organization):
    return bool(actor and actor.is_authenticated
        and get_user_model().objects.filter(pk=actor.pk, is_active=True, is_superuser=False).exists()
        and OrganizationAccess.objects.filter(user=actor, organization=organization, role__in=['owner', 'admin']).exists()
        and not Employee.objects.filter(user=actor, organization=organization).filter(
            Q(is_active=False) | Q(employment_status='dismissed')).exists())


def dismissal_preview(actor, employee):
    if not manages_staff(actor, employee.organization):
        raise PermissionDenied
    from pool_service.crm_commercial_models import CrmLead, CrmDeal
    return {
        'leads': CrmLead.objects.filter(organization=employee.organization, assignee_id=employee.user_id,
                                      is_archived=False).exclude(status='closed') if employee.user_id else CrmLead.objects.none(),
        'deals': CrmDeal.objects.filter(organization=employee.organization, manager_id=employee.user_id,
                                      is_archived=False, outcome='open') if employee.user_id else CrmDeal.objects.none(),
    }


def dismissal_recipients(organization):
    from .crm_permissions import crm_staff_users
    from pool_service.crm_commercial_models import CrmAccess
    people = StaffAccount.objects.filter(organization=organization, kind='person').values('user_id')
    employees = Employee.objects.filter(organization=organization, is_active=True).exclude(
        employment_status='dismissed').values('user_id')
    owners = OrganizationAccess.objects.filter(organization=organization, role='owner').values('user_id')
    grants = CrmAccess.objects.filter(organization=organization).filter(
        Q(edit_own_leads=True) | Q(transition_own_leads=True) | Q(create_own_leads=True) | Q(take_leads=True)
        | Q(edit_own_deals=True) | Q(transition_own_deals=True) | Q(create_own_deals=True)
        | Q(manage_own_next_action=True)).values('user_id')
    return crm_staff_users(organization).filter(pk__in=grants).filter(
        Q(pk__in=people) | Q(pk__in=employees) | Q(pk__in=owners))


@transaction.atomic
def dismiss_employee(*, actor, employee_id, expected_updated_at, dismissed_at=None, recipient_id=None):
    peek = Employee.objects.get(pk=employee_id)
    org = Organization.objects.select_for_update().get(pk=peek.organization_id)
    if not manages_staff(actor, org):
        raise PermissionDenied
    employee = Employee.objects.select_for_update().get(pk=employee_id, organization=org)
    if employee.updated_at.isoformat() != expected_updated_at:
        raise ValidationError('Карточка изменилась. Проверьте список ещё раз.')
    dismissed_at = dismissed_at or employee.dismissed_at
    if dismissed_at and (dismissed_at > timezone.localdate() or employee.hired_at and dismissed_at < employee.hired_at):
        raise ValidationError('Проверьте дату увольнения.')
    if employee.user_id == actor.pk or employee.user_id and OrganizationAccess.objects.filter(
            user_id=employee.user_id, organization=org, role='owner').exists():
        raise ValidationError('Нельзя уволить себя или владельца организации.')
    account = StaffAccount.objects.filter(organization=org, user_id=employee.user_id).first()
    if account and account.kind in {'system', 'service'}:
        raise ValidationError('Служебная запись не является сотрудником.')
    if employee.user_id and get_user_model().objects.filter(pk=employee.user_id, is_superuser=True).exists():
        raise ValidationError('Техническую административную запись нельзя увольнять как сотрудника.')
    recipient = None
    if recipient_id:
        recipient = dismissal_recipients(org).filter(pk=recipient_id).first()
        if recipient is None or recipient.pk == employee.user_id:
            raise ValidationError('Получатель недоступен в этой организации.')
    preview = dismissal_preview(actor, employee)
    # Existing validated instance writes retain all CRM links and event authors.
    from pool_service.crm_commercial_models import CrmEvent
    from .crm_assignment_notifications import notify_assignment
    counts = {}
    for key, field in [('leads', 'assignee'), ('deals', 'manager')]:
        count = 0
        for card in preview[key].select_for_update().order_by('pk'):
            if recipient:
                from .crm_assignments import assignment_recipient
                assignment_recipient('default', org, recipient.pk,
                    'lead' if key == 'leads' else 'deal', subject=card)
            before = getattr(card, field + '_id')
            setattr(card, field, recipient)
            old_revision = card.revision
            card.revision += 1
            card.save()
            event = CrmEvent.objects.create(organization=org, actor=actor,
                **{'lead' if key == 'leads' else 'deal': card}, event_type='staff.reassigned',
                safe_diff={field + '_id': {'before': before, 'after': recipient.pk if recipient else None},
                           'revision': {'before': old_revision, 'after': card.revision}})
            if recipient:
                notify_assignment(event, recipient)
            count += 1
        counts[key] = count
    roles = list(OrganizationAccess.objects.filter(organization=org, user_id=employee.user_id).values_list('role', flat=True)) if employee.user_id else []
    before_status = {'employment_status': employee.employment_status, 'is_active': employee.is_active,
        'dismissed_at': employee.dismissed_at.isoformat() if employee.dismissed_at else None}
    employee.employment_status = 'dismissed'
    employee.is_active = False
    employee.dismissed_at = dismissed_at
    employee.save(update_fields=['employment_status', 'is_active', 'dismissed_at', 'updated_at'])
    ended_at = timezone.now()
    if dismissed_at:
        ended_at = min(ended_at, timezone.make_aware(datetime.combine(dismissed_at + timedelta(days=1), time.min)))
    EmployeeTelephoneAssignment.objects.filter(employee=employee, revoked_at__isnull=True).update(revoked_at=ended_at)
    EmployeeTelephoneAssignment.objects.filter(employee=employee, valid_from__lt=ended_at).filter(
        Q(valid_to__isnull=True) | Q(valid_to__gt=ended_at)).update(valid_to=ended_at)
    pool_roles = list(PoolAccess.objects.filter(user_id=employee.user_id, pool__organization=org).values('pool_id', 'role')) if employee.user_id else []
    client_roles = list(ClientAccess.objects.filter(user_id=employee.user_id, client__organization=org).values('client_id', 'role')) if employee.user_id else []
    DataAuditLog.objects.create(entity_type='Employee', entity_id=str(employee.pk), organization=org,
        actor=actor, action='update', before={**before_status, 'roles': roles, 'pool_access': pool_roles, 'client_access': client_roles},
        after={'employment_status': 'dismissed', 'is_active': False,
            'dismissed_at': dismissed_at.isoformat() if dismissed_at else None, 'recipient_id': recipient_id, **counts},
        changed_fields=['employment_status', 'organization_access', 'open_crm_assignment'])
    if employee.user_id:
        # Remove grants, never the identity they refer to. Save the grants in audit.
        OrganizationAccess.objects.filter(organization=org, user_id=employee.user_id).delete()
        PoolAccess.objects.filter(user_id=employee.user_id, pool__organization=org).delete()
        ClientAccess.objects.filter(user_id=employee.user_id, client__organization=org).delete()
        if not (OrganizationAccess.objects.filter(user_id=employee.user_id).exists()
                or PoolAccess.objects.filter(user_id=employee.user_id).exists()
                or ClientAccess.objects.filter(user_id=employee.user_id).exists()
                or Client.objects.filter(user_id=employee.user_id).exists()):
            user = get_user_model().objects.select_for_update().get(pk=employee.user_id)
            user.is_active = False
            user.save(update_fields=['is_active'])
            session_ids = [s.pk for s in Session.objects.filter(expire_date__gt=timezone.now()).iterator(chunk_size=200)
                           if str(s.get_decoded().get('_auth_user_id')) == str(user.pk)]
            Session.objects.filter(pk__in=session_ids).delete()
    return employee


@transaction.atomic
def assign_phone(*, actor, employee, connection, phone_key='', extension='', provider_user='', valid_from, valid_to=None):
    org = Organization.objects.select_for_update().get(pk=employee.organization_id)
    employee = Employee.objects.select_for_update().get(pk=employee.pk, organization=org)
    if timezone.is_naive(valid_from) or valid_to and timezone.is_naive(valid_to):
        raise ValidationError('Укажите часовой пояс назначения номера.')
    if not manages_staff(actor, org):
        raise PermissionDenied
    if connection.organization_id != org.pk or not employee.is_active or employee.employment_status == 'dismissed':
        raise ValidationError('Проверьте сотрудника и организацию АТС.')
    if not phone_key and not extension and not provider_user:
        raise ValidationError('Укажите номер или идентификатор АТС.')
    query = Q(pk__in=[])
    for field, value in [('phone_key', phone_key), ('extension', extension), ('provider_user', provider_user)]:
        if value:
            query |= Q(**{field: value})
    for previous in EmployeeTelephoneAssignment.objects.filter(organization=org, connection=connection).filter(query):
        ends = [v for v in [previous.valid_to, previous.revoked_at] if v]
        end = min(ends) if ends else None
        if end and end <= previous.valid_from:
            continue
        if (end is None or end > valid_from) and (valid_to is None or previous.valid_from < valid_to):
            raise ValidationError('Интервал пересекается с существующим назначением номера. Сначала закройте его.')
    row = EmployeeTelephoneAssignment(organization=org, employee=employee, connection=connection,
        phone_key=phone_key, extension=extension, provider_user=provider_user, valid_from=valid_from,
        valid_to=valid_to, confirmed_by=actor)
    row.full_clean()
    row.save()
    DataAuditLog.objects.create(organization=org, actor=actor, entity_type='EmployeeTelephoneAssignment',
        entity_id=str(row.pk), action='create', after={'employee_id': employee.pk},
        changed_fields=['telephone_assignment'])
    return row


@transaction.atomic
def close_phone(*, actor, assignment_id, valid_to):
    peek = EmployeeTelephoneAssignment.objects.get(pk=assignment_id)
    org = Organization.objects.select_for_update().get(pk=peek.organization_id)
    if not manages_staff(actor, org):
        raise PermissionDenied
    row = EmployeeTelephoneAssignment.objects.select_for_update().get(pk=assignment_id)
    if row.valid_to is not None or row.revoked_at is not None:
        raise ValidationError('Назначение уже закрыто или отозвано. Обновите страницу.')
    row.valid_to = valid_to
    row.full_clean()
    row.save(update_fields=['valid_to'])
    DataAuditLog.objects.create(organization=org, actor=actor, entity_type='EmployeeTelephoneAssignment',
        entity_id=str(row.pk), action='update', after={'valid_to': valid_to.isoformat()},
        changed_fields=['valid_to'])
    return row
