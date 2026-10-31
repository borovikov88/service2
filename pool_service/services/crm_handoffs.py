"""Explicit acceptance of a packet for an existing service request.

No task creation/assignment, project start, contract terms, or financial writes.
"""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from pool_service.models import CrmItem, ServiceTask, Pool, OrganizationAccess, Employee
from pool_service.crm_commercial_models import CANONICAL_KEY, CrmEvent, CrmAccess
from pool_service.crm_quote_models import CrmQuote
from pool_service.crm_handoff_models import CrmHandoff, CrmHandoffTask, CrmHandoffEvent
from . import crm_quotes as quotes
from .crm_commercial_commands import _locked_organization, _authorize, _owned_deal, CommercialConflict
from .crm_permissions import crm_items_for_user, crm_staff_users, require_crm_graph_scope
from .crm_locking import lock_crm_graph
from .task_read_permissions import readable_tasks
from .task_feedback import snapshot as feedback_snapshot
from .task_feedback_permissions import lock_feedback_actor
from .permissions import company_has_access


def task_snapshot(task):
    return dict(feedback_snapshot(task), payload_hash=quotes.digest(task.payload_json), **{key:getattr(task,key) for key in (
        'organization_id','client_id','pool_id','crm_item_id','visibility','task_type','source_type','water_reading_id')})


def item_snapshot(item):
    return {key:getattr(item,key) for key in ('organization_id','client_id','pool_id',
        'direction','title','description','service_works','equipment_replacement','responsible_id','stage','is_archived')}


def eligible_context(actor, organization, deal_id, item_id, task_ids, recipient_id):
    if any(type(v) is not int or v<=0 for v in (deal_id,item_id,recipient_id)):
        raise ValidationError('Некорректный идентификатор.')
    deal=quotes.read_deal(actor,organization,deal_id)
    if deal.fulfillment_kind!='service':
        raise ValidationError('Этот срез передаёт только существующую сервисную заявку. Запуск строительства требует проверенной персональной полномочности Александра; поставка и смешанное исполнение пока не поддержаны.')
    if deal.is_archived or deal.outcome!='open' or not deal.client_id or deal.object_kind!='pool' or not deal.pool_id:
        raise ValidationError('Нужна открытая сделка обслуживания с клиентом и существующим бассейном.')
    if not Pool.objects.filter(pk=deal.pool_id,organization=organization,client_id=deal.client_id).exists():
        raise PermissionDenied('Объект больше не соответствует клиенту и организации.')
    quote=CrmQuote.objects.select_related('version__estimate').filter(deal=deal,status='accepted').first()
    if not quote: raise ValidationError('Сначала явно примите конкретную версию КП.')
    snapshot=quotes.source(quote,deal)
    errors=quotes.readiness(snapshot,quote.variant)
    if errors: raise ValidationError(errors)
    if type(task_ids) is not list or not task_ids or len(task_ids)>50 or any(type(v) is not int or v<=0 for v in task_ids) or len(set(task_ids))!=len(task_ids):
        raise ValidationError('Выберите от 1 до 50 различных существующих задач.')
    item=crm_items_for_user(actor,organization).filter(pk=item_id,client_id=deal.client_id,pool_id=deal.pool_id,direction=CrmItem.DIRECTION_SERVICE,is_archived=False).first()
    if not item or item.stage==CrmItem.STAGE_SERVICE_DONE: raise PermissionDenied('Действующая сервисная заявка недоступна или не соответствует клиенту и объекту.')
    require_crm_graph_scope([item.pk])
    recipient=crm_staff_users(organization).filter(pk=recipient_id).first()
    if not recipient: raise PermissionDenied('Получатель недоступен.')
    # Submission never grants access to a proposal, request, or task.
    quotes.read_deal(recipient,organization,deal_id)
    if not crm_items_for_user(recipient,organization).filter(pk=item.pk).exists(): raise PermissionDenied
    query=ServiceTask.objects.filter(pk__in=task_ids,organization=organization,crm_item=item,
        client_id=deal.client_id,pool_id=deal.pool_id,is_archived=False,completed_at__isnull=True).exclude(status__in=['done','cancelled'])
    tasks=readable_tasks(query,actor)
    if len(tasks)!=len(task_ids) or len(readable_tasks(query,recipient))!=len(task_ids):
        raise PermissionDenied('Каждая задача должна уже принадлежать заявке и быть доступна отправителю и получателю.')
    if any(not t.responsibles.filter(pk=recipient.pk).exists() for t in tasks):
        raise PermissionDenied('Получатель должен уже быть участником каждой выбранной задачи. Передача не назначает исполнителей.')
    return deal,quote,item,sorted(tasks,key=lambda t:t.pk),recipient


def preview(*, actor, organization, deal_id, item_id, task_ids, recipient_id, document_note):
    if type(document_note) is not str or not document_note.strip() or len(document_note)>1000:
        raise ValidationError('Укажите проверенные документы и условия передачи, до 1000 символов.')
    deal,quote,item,tasks,recipient=eligible_context(actor,organization,deal_id,item_id,task_ids,recipient_id)
    if deal.manager_id!=actor.pk: raise PermissionDenied
    if any(t.get_end_date()<t.start_date for t in tasks):
        raise ValidationError('Плановое окончание задачи не может быть раньше начала.')
    return {'deal':deal.pk,'revision':deal.revision,'quote':quote.pk,'quote_revision':quote.revision,
        'source_hash':quote.source_hash,'scope':quote.scope,'variant':quote.variant,
        'item':item.pk,'item_snapshot':item_snapshot(item),'recipient':recipient.pk,
        'tasks':{str(t.pk):task_snapshot(t) for t in tasks},'document_note':document_note.strip()}


def current_packet(actor, organization, handoff):
    """No stale content is exposed when any current source permission is absent."""
    ids=list(handoff.task_links.values_list('task_id',flat=True))
    deal,quote,item,tasks,recipient=eligible_context(actor,organization,handoff.deal_id,handoff.item_id,ids,handoff.recipient_id)
    if (quote.pk!=handoff.quote_id or quote.revision!=handoff.basis['quote_revision']
            or quote.scope!=handoff.basis['scope'] or quote.source_hash!=handoff.basis['source_hash']
            or item_snapshot(item)!=handoff.basis['item_snapshot']
            or {str(t.pk):task_snapshot(t) for t in tasks}!=handoff.basis['tasks']):
        raise CommercialConflict('Основание передачи изменилось. Требуется сверка; прежняя приёмка не подтверждает новый состав. Повторная передача в этом срезе недоступна, история сохранена.')
    return deal,quote,item,tasks,recipient


@transaction.atomic
def execute(*, actor, organization_id, deal_id, action, operation_key, basis=None, expected_revision=None, evidence=''):
    if type(operation_key) is not str or not operation_key or len(operation_key)>100: raise ValidationError('Некорректный ключ операции.')
    CANONICAL_KEY(operation_key)
    if action not in {'submit','accept','decline'} or type(evidence) is not str or not evidence.strip() or len(evidence)>1000:
        raise ValidationError('Укажите действие и основание решения, до 1000 символов.')
    if type(organization_id) is not int or type(deal_id) is not int or organization_id<=0 or deal_id<=0:
        raise ValidationError('Некорректная организация или сделка.')
    org=_locked_organization('default',organization_id)
    if not org.crm_v2_write or not company_has_access(org): raise PermissionDenied
    deal=quotes.read_deal(actor,org,deal_id)
    handoff=CrmHandoff.objects.select_for_update().filter(deal=deal).first()
    if action=='submit':
        if type(basis) is not dict or type(basis.get('item')) is not int or type(basis.get('tasks')) is not dict:
            raise ValidationError('Нужно актуальное подтверждение просмотренного пакета.')
        item_id=basis['item']
    else:
        if not handoff or actor.pk!=handoff.recipient_id: raise PermissionDenied
        item_id=handoff.item_id
    if not crm_items_for_user(actor,org).filter(pk=item_id).exists(): raise PermissionDenied
    # Same task -> legacy CRM -> actor order as legacy feedback/completion.
    lock_crm_graph([item_id])
    recipient_id=basis.get('recipient') if action=='submit' else handoff.recipient_id
    if type(recipient_id) is not int or recipient_id<=0: raise ValidationError('Некорректный получатель.')
    participant_ids=sorted({actor.pk,recipient_id})
    participants=list(get_user_model().objects.select_for_update().filter(pk__in=participant_ids).order_by('pk'))
    if len(participants)!=len(participant_ids) or any(not user.is_active for user in participants): raise PermissionDenied
    for model in (OrganizationAccess,Employee,CrmAccess):
        list(model.objects.select_for_update().filter(organization=org,user_id__in=participant_ids).order_by('pk'))
    if action=='submit':
        actor=_authorize('default',actor.pk,org,'edit_own_deals')
        deal=_owned_deal('default',actor.pk,org.pk,deal_id)
        try:
            fresh=preview(actor=actor,organization=org,deal_id=deal_id,item_id=item_id,
                task_ids=[int(v) for v in basis['tasks']],recipient_id=basis['recipient'],document_note=basis['document_note'])
        except (KeyError,TypeError,ValueError) as exc: raise ValidationError('Некорректный пакет.') from exc
        # Replay validates current sources too, but does not require the old deal revision.
        old=CrmHandoffEvent.objects.filter(organization=org,operation_key=operation_key).first()
        payload=quotes.digest(dict(actor=actor.pk,deal=deal_id,action=action,basis=basis,evidence=evidence))
        if old:
            if old.actor_id!=actor.pk or old.payload_hash!=payload: raise CommercialConflict('Ключ уже использован.')
            current_packet(actor,org,old.handoff)
            return old.handoff
        if fresh!=basis: raise CommercialConflict('Пакет или сроки изменились после просмотра. Обновите предварительный просмотр.')
        if handoff or CrmHandoff.objects.filter(item_id=item_id).exists() or CrmHandoffTask.objects.filter(task_id__in=[int(v) for v in basis['tasks']]).exists():
            raise CommercialConflict('Передача уже существует. Повторное назначение того же исполнения не создаётся.')
        handoff=CrmHandoff.objects.create(organization=org,deal=deal,quote_id=basis['quote'],item_id=item_id,
            recipient_id=basis['recipient'],submitted_by=actor,basis=basis)
        for task_id,snapshot in basis['tasks'].items():
            CrmHandoffTask.objects.create(organization=org,handoff=handoff,task_id=int(task_id),snapshot=snapshot)
    else:
        deal,quote,item,tasks,recipient=current_packet(actor,org,handoff)
        # Acceptance records the recipient's consent under existing task write authority.
        for task in tasks: actor=lock_feedback_actor(task,actor)
        payload=quotes.digest(dict(actor=actor.pk,deal=deal_id,action=action,revision=expected_revision,evidence=evidence))
        old=CrmHandoffEvent.objects.filter(organization=org,operation_key=operation_key).first()
        if old:
            if old.actor_id!=actor.pk or old.payload_hash!=payload: raise CommercialConflict('Ключ уже использован.')
            return handoff
        if type(expected_revision) is not int or handoff.revision!=expected_revision or handoff.status!='pending':
            raise CommercialConflict('Приёмка уже изменена. Обновите страницу.')
        handoff.status='accepted' if action=='accept' else 'declined'
        handoff.revision+=1;handoff.save()
    CrmHandoffEvent.objects.create(organization=org,handoff=handoff,actor=actor,operation_key=operation_key,
        payload_hash=payload,action=action,evidence=evidence.strip())
    CrmEvent.objects.create(organization=org,deal=deal,actor=actor,event_type='handoff.'+action)
    return handoff
