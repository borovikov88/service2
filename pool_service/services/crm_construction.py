"""Explicit first launch of an existing project, without payment or task creation."""
from datetime import date
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from pool_service.models import CrmItem
from pool_service.crm_commercial_models import CANONICAL_KEY, CrmEvent
from pool_service.crm_construction_models import CrmConstructionAuthority, CrmConstructionLaunch
from . import crm_quotes as quotes
from .crm_handoffs import _locked_context, item_snapshot, task_snapshot
from .crm_commercial_commands import _locked_organization, CommercialConflict
from .crm_locking import lock_crm_graph
from .crm_permissions import crm_items_for_user
from .permissions import company_has_access


def has_authority(actor, org):
    return bool(actor.is_authenticated and actor.is_active and org.crm_v2_write and
        CrmConstructionAuthority.objects.filter(organization=org,user=actor,enabled=True).exists())


def _context(actor, org, deal_id, project_id, task_ids, recipient_id):
    if any(type(v) is not int or v <= 0 for v in (deal_id,project_id,recipient_id)):
        raise ValidationError('Некорректный идентификатор.')
    if (type(task_ids) is not list or len(task_ids)>50
            or any(type(v) is not int or v<=0 for v in task_ids) or len(set(task_ids))!=len(task_ids)):
        raise ValidationError('Выберите не более 50 существующих задач.')
    quotes.read_deal(actor,org,deal_id)
    if not crm_items_for_user(actor,org).filter(pk=project_id).exists(): raise PermissionDenied
    graph=lock_crm_graph([project_id],extra_task_ids=task_ids)
    return _locked_context(actor.pk,org,deal_id,project_id,task_ids,recipient_id,graph,construction=True)


def _basis(context, planned_start, planned_end, document_note):
    if (type(planned_start) is not date or type(planned_end) is not date or planned_end<planned_start):
        raise ValidationError('Укажите корректные плановые даты.')
    if type(document_note) is not str or not document_note.strip() or len(document_note)>1000:
        raise ValidationError('Укажите проверенные документы, до 1000 символов.')
    deal,quote,project,tasks,recipient=context
    return dict(deal=deal.pk,revision=deal.revision,quote=quote.pk,quote_revision=quote.revision,
        version=quote.version_id,variant=quote.variant,source_hash=quote.source_hash,scope=quote.scope,
        project=project.pk,project_snapshot=item_snapshot(project),recipient=recipient.pk,
        planned_start=planned_start.isoformat(),planned_end=planned_end.isoformat(),document_note=document_note.strip(),
        tasks={str(t.pk):task_snapshot(t) for t in tasks})


def _require_authority(actor, org):
    # No admin, superuser, manager, name or task-assignee fallback.
    if not org.crm_v2_write or not company_has_access(org): raise PermissionDenied
    grant=CrmConstructionAuthority.objects.select_for_update().filter(organization=org,user=actor).first()
    if not grant or not grant.enabled: raise PermissionDenied('Персональное право запуска не настроено.')


def _require_first_launch(project):
    if project.stage not in {CrmItem.STAGE_PROJECT_IDEA,CrmItem.STAGE_PROJECT_DESIGN}:
        raise CommercialConflict('Проект уже выполняется, завершён или приостановлен. Нужна отдельная сверка истории запуска.')


@transaction.atomic
def preview(*, actor, organization_id, deal_id, project_id, task_ids, recipient_id,
            planned_start, planned_end, document_note):
    org=_locked_organization('default',organization_id)
    context=_context(actor,org,deal_id,project_id,task_ids,recipient_id)
    _require_authority(actor,org)
    _require_first_launch(context[2])
    if CrmConstructionLaunch.objects.select_for_update().filter(project_id=project_id).exists() or CrmConstructionLaunch.objects.select_for_update().filter(deal_id=deal_id).exists():
        raise CommercialConflict('Запуск уже зарегистрирован.')
    return _basis(context,planned_start,planned_end,document_note)


@transaction.atomic
def launch(*, actor, organization_id, deal_id, basis, operation_key, evidence, confirm_first_launch=False):
    if type(operation_key) is not str or not operation_key or len(operation_key)>100:
        raise ValidationError('Некорректный ключ операции.')
    CANONICAL_KEY(operation_key)
    if type(evidence) is not str or not evidence.strip() or len(evidence)>1000 or confirm_first_launch is not True:
        raise ValidationError('Подтвердите первый запуск и укажите основание решения.')
    if type(basis) is not dict or basis.get('deal')!=deal_id or type(basis.get('tasks')) is not dict:
        raise ValidationError('Нужно подтверждение просмотренного пакета.')
    try:
        ids=[int(v) for v in basis['tasks']]
        start=date.fromisoformat(basis['planned_start']);end=date.fromisoformat(basis['planned_end'])
        project_id=basis['project'];recipient_id=basis['recipient'];note=basis['document_note']
    except (ValueError,TypeError,KeyError) as exc: raise ValidationError('Некорректный пакет.') from exc
    org=_locked_organization('default',organization_id)
    context=_context(actor,org,deal_id,project_id,ids,recipient_id)
    _require_authority(actor,org)
    fresh=_basis(context,start,end,note)
    payload=quotes.digest(dict(actor=actor.pk,deal=deal_id,basis=basis,evidence=evidence,confirm_first_launch=True))
    old=CrmConstructionLaunch.objects.select_for_update().filter(organization=org,operation_key=operation_key).first()
    if old:
        if old.actor_id!=actor.pk or old.payload_hash!=payload or old.deal_id!=deal_id:
            raise CommercialConflict('Ключ уже использован.')
        # Replays still require current source access/integrity, but never restart a project.
        for key in ('quote','quote_revision','version','variant','source_hash','scope'):
            if fresh[key]!=old.basis[key]: raise CommercialConflict('Основание запуска изменилось. Нужна сверка; история сохранена.')
        return old
    _require_first_launch(context[2])
    if fresh!=basis: raise CommercialConflict('Пакет изменился после просмотра. Обновите подтверждение.')
    if CrmConstructionLaunch.objects.select_for_update().filter(project_id=project_id).exists() or CrmConstructionLaunch.objects.select_for_update().filter(deal_id=deal_id).exists():
        raise CommercialConflict('Сделка или проект уже переданы в работу.')
    record=CrmConstructionLaunch.objects.create(organization=org,deal_id=deal_id,project_id=project_id,
        quote_id=basis['quote'],actor=actor,recipient_id=recipient_id,basis=basis,evidence=evidence.strip(),
        operation_key=operation_key,payload_hash=payload)
    project=context[2]
    project.stage=CrmItem.STAGE_PROJECT_BUILD;project.responsible_id=recipient_id
    project.save(update_fields=['stage','responsible','updated_at'])
    CrmEvent.objects.create(organization=org,deal_id=deal_id,actor=actor,event_type='construction.launched')
    return record


def visible_record(actor, org, record):
    # Use current permissions before exposing frozen commercial/task evidence.
    with transaction.atomic():
        locked_org=_locked_organization('default',org.pk)
        context=_context(actor,locked_org,record.deal_id,record.project_id,
            [int(v) for v in record.basis['tasks']],record.recipient_id)
        deal,quote,project,tasks,recipient=context
        if any(record.basis[key]!=value for key,value in (
            ('quote',quote.pk),('quote_revision',quote.revision),('source_hash',quote.source_hash),('scope',quote.scope))):
            raise CommercialConflict('Основание изменилось; требуется сверка.')
        changed = (project.responsible_id != record.recipient_id or
            {str(t.pk):task_snapshot(t) for t in tasks} != record.basis['tasks'])
        return dict(project=project,recipient=recipient,changed=changed,
            rows=quotes.selected_rows(quote.version.snapshot,quote.variant))
