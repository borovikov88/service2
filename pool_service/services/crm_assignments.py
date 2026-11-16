"""Explicit CRM responsibility and optional canonical next-action handoff."""
import hashlib
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from pool_service.crm_commercial_models import (CANONICAL_KEY, CrmAccess, CrmCommand, CrmDeal,
    CrmDealNextAction, CrmDealTaskLink, CrmEvent, CrmLead, CrmLeadDealLink, CrmLeadTaskLink)
from pool_service.models import Employee, Organization, OrganizationAccess, ServiceTask, ServiceTaskChange
from .crm_commercial_commands import CommercialConflict, _authorize, _positive_id
from .crm_commercial_access import can_view_team_deals
from .crm_lead_access import can_read_lead, can_view_leads
from .permissions import company_has_access


ASSIGNMENT_NOTE = 'Можно передать текущее следующее действие вместе с карточкой. Остальные задачи и их ответственные сохраняются.'


def _current_task(subject, *, lock=False):
    if isinstance(subject, CrmLead):
        task_id = subject.next_task_id
    else:
        pointers = CrmDealNextAction.objects.filter(deal=subject, organization_id=subject.organization_id)
        if lock:
            pointers = pointers.select_for_update()
        task_id = pointers.values_list('task_link__task_id', flat=True).first()
    tasks = ServiceTask.objects.filter(pk=task_id)
    if lock:
        tasks = tasks.select_for_update()
    return tasks.first()


def _task_problem(subject, task, actor, *, lock=False):
    from .task_read_permissions import readable_tasks
    if not task or not readable_tasks(ServiceTask.objects.filter(pk=task.pk), actor):
        return 'Следующее действие недоступно для передачи.'
    owner_id = subject.assignee_id if isinstance(subject, CrmLead) else subject.manager_id
    payload = task.payload_json or {}
    if not isinstance(payload, dict):
        return 'Связь следующего действия требует проверки.'
    marker = payload.get('crm_lead') if isinstance(subject, CrmLead) else payload.get('crm_commercial') or payload.get('crm_deal')
    key = 'lead_id' if isinstance(subject, CrmLead) else 'deal_id'
    responsible_rows = task.responsibles.select_for_update() if lock else task.responsibles
    if (task.organization_id != subject.organization_id or task.client_id != subject.client_id
            or task.task_type != ServiceTask.TYPE_CRM_FOLLOWUP or task.source_type != ServiceTask.SOURCE_MANAGER
            or task.pool_id or task.water_reading_id or task.crm_item_id or task.parent_task_id
            or task.visibility != ServiceTask.VISIBILITY_PRIVATE
            or not isinstance(marker, dict) or marker.get(key) != subject.pk
            or set(payload) - {'crm_lead', 'crm_commercial', 'crm_deal', 'source_call_id'}
            or task.primary_responsible_id != owner_id or set(responsible_rows.values_list('pk', flat=True)) != {owner_id}
            or CrmLead.objects.filter(next_task_id=task.pk).exclude(pk=subject.pk if isinstance(subject, CrmLead) else None).exists()
            or CrmDealNextAction.objects.filter(task_link__task_id=task.pk).exclude(deal_id=subject.pk if isinstance(subject, CrmDeal) else None).exists()):
        return 'Эту задачу нужно передать отдельно: она имеет другие связи или исполнителей.'
    expected_link = CrmLeadTaskLink.objects.filter(lead=subject, task=task, organization_id=subject.organization_id) if isinstance(subject, CrmLead) else CrmDealTaskLink.objects.filter(deal=subject, task=task, organization_id=subject.organization_id)
    if not expected_link.exists():
        return 'Связь следующего действия требует проверки.'
    if (not task.is_editable or task.is_archived or task.completed_at
            or task.status in {ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED, ServiceTask.STATUS_WAITING}):
        return 'Завершённая или ожидающая задача сохраняет текущего исполнителя.'
    if lock:
        from .task_feedback_permissions import lock_feedback_actor
        lock_feedback_actor(task, actor)
    return ''


def assignment_task_data(subject, actor):
    from .task_feedback import version_for
    task = _current_task(subject)
    if not task:
        return None
    reason = _task_problem(subject, task, actor)
    if reason == 'Следующее действие недоступно для передачи.':
        return {'can_transfer': False, 'reason': reason}
    return {'id': task.pk, 'title': task.title, 'version': version_for(task),
        'can_transfer': not reason, 'reason': reason}


def _transfer_task(subject, task, actor, recipient, expected_version):
    from .task_feedback import version_for
    reason = _task_problem(subject, task, actor, lock=True)
    if reason:
        raise ValidationError(reason)
    if expected_version != version_for(task):
        raise CommercialConflict('Следующее действие изменилось. Откройте карточку заново.')
    call_id = (task.payload_json or {}).get('source_call_id')
    if call_id:
        from .crm_lead_access import has_readable_family_call
        leads = [subject] if isinstance(subject, CrmLead) else [link.lead for link in CrmLeadDealLink.objects.filter(deal=subject, organization_id=subject.organization_id).select_related('lead')]
        if not any(can_read_lead(recipient, subject.organization, lead)
                and has_readable_family_call(recipient, subject.organization, lead, call_id) for lead in leads):
            raise PermissionDenied('У нового ответственного нет доступа к источнику текущей задачи. Снимите передачу задачи или выберите другого сотрудника.')
    old_id = task.primary_responsible_id
    task.primary_responsible = recipient
    task.save(update_fields=['primary_responsible', 'updated_at'])
    task.responsibles.set([recipient])
    ServiceTaskChange.objects.create(task=task, changed_by=actor, action=ServiceTaskChange.ACTION_UPDATED,
        field_name='crm_responsible', old_value=str(old_id), new_value=str(recipient.pk))


def _readable(user, organization, kind, subject=None, *, lock=False):
    if kind == 'lead':
        return can_read_lead(user, organization, subject) if subject else can_view_leads(user, organization)
    if not can_view_team_deals(user, organization):
        return False
    if subject:
        from .crm_deal_objects import validate_deal_object
        try:
            validate_deal_object('default', user, subject, lock=lock)
        except (PermissionDenied, ValidationError):
            return False
    return True


def assignment_available(user, organization, subject, *, lock=False):
    kind = 'lead' if isinstance(subject, CrmLead) else 'deal'
    if subject.is_archived or (subject.status == 'closed' if kind == 'lead' else subject.outcome != 'open'):
        return False
    if not _readable(user, organization, kind, subject, lock=lock):
        return False
    grant = CrmAccess.objects.filter(organization=organization, user_id=user.pk).first()
    if not organization.crm_v2_write or not grant or not getattr(grant, 'edit_own_' + ('leads' if kind == 'lead' else 'deals')):
        return False
    owner_id = subject.assignee_id if kind == 'lead' else subject.manager_id
    return owner_id == user.pk or OrganizationAccess.objects.filter(organization=organization,
        user_id=user.pk, role__in=('owner', 'admin')).exists()


def assignment_recipient(using, organization, responsible_id, kind, *, subject=None, source_lead=None):
    """Current membership, working status and actual source/object visibility."""
    _positive_id(responsible_id)
    user = get_user_model().objects.using(using).select_for_update().filter(pk=responsible_id, is_active=True).first()
    memberships = list(OrganizationAccess.objects.using(using).select_for_update().filter(
        organization=organization, user_id=responsible_id))
    employees = list(Employee.objects.using(using).select_for_update().filter(organization=organization, user_id=responsible_id))
    grant = CrmAccess.objects.using(using).select_for_update().filter(organization=organization, user_id=responsible_id).first()
    capabilities = ('edit_own_leads', 'transition_own_leads', 'create_own_leads', 'take_leads') if kind == 'lead' else (
        'edit_own_deals', 'transition_own_deals', 'create_own_deals', 'manage_own_next_action')
    if (not user or not memberships or not grant or not any(getattr(grant, key) for key in capabilities)
            or any(not employee.is_active or employee.employment_status == Employee.STATUS_DISMISSED for employee in employees)
            or not _readable(user, organization, kind, subject, lock=True)
            or source_lead is not None and not can_read_lead(user, organization, source_lead)):
        raise PermissionDenied('Сотрудник недоступен или не имеет доступа к этой карточке CRM и её источникам.')
    return user


def _actor(using, actor, organization, kind, *, create=False):
    cap = ('create_own_' if create else 'edit_own_') + ('leads' if kind == 'lead' else 'deals')
    if kind == 'lead':
        from .crm_lead_commands import _authorize_lead
        actor = _authorize_lead(using, actor.pk, organization, 'lead.create' if create else 'lead.edit')
    else:
        actor = _authorize(using, actor.pk, organization, cap)
    if not company_has_access(organization):
        raise PermissionDenied
    return actor


def responsible_candidates(*, actor, organization, kind, subject_id=None, lead_id=None):
    if kind not in {'lead', 'deal'} or subject_id is not None and lead_id is not None:
        raise ValidationError('Выберите лид или сделку.')
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        actor = _actor('default', actor, organization, kind, create=subject_id is None)
        subject = None
        if subject_id is not None:
            model = CrmLead if kind == 'lead' else CrmDeal
            subject = model.objects.select_for_update().filter(pk=subject_id, organization=organization).first()
            if not subject or not _readable(actor, organization, kind, subject, lock=True) or not assignment_available(actor, organization, subject, lock=True):
                raise PermissionDenied
        source_lead = None
        if lead_id is not None:
            source_lead = CrmLead.objects.select_for_update().filter(pk=lead_id, organization=organization).first()
            if not source_lead or not can_read_lead(actor, organization, source_lead):
                raise PermissionDenied
        ids = get_user_model().objects.filter(is_active=True,
            organizationaccess__organization=organization).order_by('first_name', 'last_name', 'username').values_list('pk', flat=True).distinct()
        users = []
        for user_id in ids:
            try:
                recipient = assignment_recipient('default', organization, user_id, kind, subject=subject, source_lead=source_lead)
            except PermissionDenied:
                continue
            users.append({'id': recipient.pk, 'name': recipient.get_full_name() or recipient.username})
        return {'users': users, 'current_user_id': actor.pk, 'assignment_note': ASSIGNMENT_NOTE}


def execute_assignment(*, actor, organization, operation_key, command_type, subject_id, expected_revision, changes):
    if (command_type not in {'lead.assign', 'deal.assign'} or type(changes) is not dict
            or 'responsible_id' not in changes or not set(changes) <= {'responsible_id', 'transfer_next_action', 'expected_task_version'}):
        raise ValidationError('Выберите нового ответственного.')
    transfer = changes.get('transfer_next_action', False)
    if type(transfer) is not bool or transfer and (type(changes.get('expected_task_version')) is not str or len(changes['expected_task_version']) != 64) or not transfer and 'expected_task_version' in changes:
        raise ValidationError('Подтвердите передачу текущего действия и его версию.')
    _positive_id(subject_id); _positive_id(changes['responsible_id'])
    if type(expected_revision) is not int or expected_revision < 0 or type(operation_key) is not str or len(operation_key) > 100:
        raise ValidationError('Некорректная версия карточки или ключ операции.')
    CANONICAL_KEY(operation_key)
    kind = command_type.split('.')[0]
    digest = hashlib.sha256(json.dumps([actor.pk, organization.pk, command_type, subject_id,
        expected_revision, changes], sort_keys=True).encode()).hexdigest()
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        model = CrmLead if kind == 'lead' else CrmDeal
        subject = model.objects.select_for_update().filter(pk=subject_id, organization=organization).first()
        task = _current_task(subject, lock=True) if subject and transfer else None
        actor = _actor('default', actor, organization, kind)
        if not subject or not _readable(actor, organization, kind, subject, lock=True):
            raise PermissionDenied
        previous = CrmCommand.objects.select_for_update().filter(organization=organization, operation_key=operation_key).first()
        if previous:
            if previous.actor_id != actor.pk or previous.command_type != command_type or previous.payload_hash != digest:
                raise CommercialConflict('Ключ операции уже использован с другими данными.')
            return {**previous.result, 'assignment_saved': True}
        if not assignment_available(actor, organization, subject, lock=True):
            raise PermissionDenied('Назначение доступно текущему ответственному или руководителю с правами CRM.')
        if subject.revision != expected_revision:
            raise CommercialConflict('Карточка изменилась. Откройте её заново.')
        recipient = assignment_recipient('default', organization, changes['responsible_id'], kind, subject=subject)
        field = 'assignee_id' if kind == 'lead' else 'manager_id'
        previous_id = getattr(subject, field)
        if previous_id == recipient.pk:
            raise ValidationError('Этот сотрудник уже является ответственным.')
        if transfer:
            _transfer_task(subject, task, actor, recipient, changes['expected_task_version'])
        setattr(subject, field, recipient.pk)
        subject.revision += 1
        subject.save()
        command = CrmCommand.objects.create(organization=organization, actor=actor, operation_key=operation_key,
            command_type=command_type, payload_hash=digest)
        event = CrmEvent.objects.create(organization=organization, actor=actor, command=command,
            **{kind: subject}, event_type=command_type, safe_diff={field: {'before': previous_id, 'after': recipient.pk},
                'revision': {'before': expected_revision, 'after': subject.revision}},
            reason='Текущее следующее действие передано новому ответственному. Остальные задачи сохранены.' if transfer else 'Ответственный карточки изменён. Исполнители существующих задач сохранены.')
        from .crm_assignment_notifications import notify_assignment
        notify_assignment(event, recipient)
        command.result = {kind + '_id': subject.pk, 'event_id': event.pk, 'revision': subject.revision}
        command.save(update_fields=['result'])
        return {**command.result, 'assignment_saved': True}
