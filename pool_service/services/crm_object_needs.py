"""Transactional source identity and explicit own-task classification.

Lock order is documented in docs/ai/crm-object-needs.md. No provider actions,
new task assignment, commercial deal, contract or fulfilment is created here.
"""
import hashlib
import json
import re
from contextlib import contextmanager

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Q

from pool_service.models import Client, CrmItem, Organization, Pool, ServiceTask, ServiceTaskChange, WaterReading
from pool_service.crm_commercial_models import CrmAccess, CrmCommand
from pool_service.crm_object_need_models import CrmObjectNeed, CrmObjectNeedReview, CrmObjectNeedSourceRevision, validate_positions
from pool_service.services.task_feedback import snapshot
from pool_service.services.task_read_permissions import readable_task_queryset, readable_tasks


class NeedConflict(ValidationError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def materials_hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


def validate_graph(reading, task, item, organization_id):
    payload = task.payload_json if isinstance(task.payload_json, dict) else {}
    if 'source_call_id' in payload or payload.get('source') == 'call_analysis':
        raise NeedConflict('У исходной заявки неоднозначное происхождение.')
    pool = reading.pool
    if (not organization_id or pool.organization_id != organization_id or pool.client.organization_id != organization_id
            or task.organization_id != organization_id or task.water_reading_id != reading.pk
            or task.task_type != ServiceTask.TYPE_SUPPLY_REQUEST or task.pool_id != pool.pk or task.client_id != pool.client_id
            or task.crm_item_id != item.pk or item.organization_id != organization_id
            or item.direction != CrmItem.DIRECTION_SERVICE or item.pool_id != pool.pk or item.client_id != pool.client_id):
        raise NeedConflict('Связи исходной заявки требуют отдельной проверки.')


def editable(task):
    item = task.crm_item
    return bool(task.is_editable and not task.is_archived and not task.completed_at
        and task.status in (ServiceTask.STATUS_NEW, ServiceTask.STATUS_IN_PROGRESS)
        and item and not item.is_archived and item.stage in (CrmItem.STAGE_SERVICE_NEW, CrmItem.STAGE_SERVICE_IN_PROGRESS))


SOURCE_GRAPH_FIELDS = ('pk', 'organization_id', 'task_type', 'water_reading_id', 'crm_item_id', 'pool_id', 'client_id')


def source_graph_queryset(reading_ids):
    from django.db.models import Subquery
    seeds = ServiceTask.objects.filter(water_reading_id__in=reading_ids,
        task_type=ServiceTask.TYPE_SUPPLY_REQUEST)
    return ServiceTask.objects.filter(
        Q(water_reading_id__in=reading_ids, task_type=ServiceTask.TYPE_SUPPLY_REQUEST)
        | Q(crm_item_id__in=Subquery(seeds.exclude(crm_item_id=None).values('crm_item_id')))
    ).order_by('pk')


def source_graph_signature(reading_ids):
    # A nonlocking recheck, including an initially empty graph. Callers must
    # not acquire newly discovered task locks after Reading/recipient locks.
    return tuple(source_graph_queryset(reading_ids).values_list(*SOURCE_GRAPH_FIELDS))


def lock_source_graphs(reading_ids, *, with_signature=False):
    # One task locking statement includes every candidate and every task
    # sharing its CRM item. Do not lock a seed task before the sorted graph.
    tasks = list(source_graph_queryset(reading_ids).select_for_update())
    items = {item.pk: item for item in CrmItem.objects.select_for_update().filter(
        pk__in={task.crm_item_id for task in tasks if task.crm_item_id}).order_by('pk')}
    candidates = {}
    for task in tasks:
        if task.water_reading_id in reading_ids and task.task_type == ServiceTask.TYPE_SUPPLY_REQUEST:
            candidates.setdefault(task.water_reading_id, []).append(task)
            if task.crm_item_id:
                task.crm_item = items[task.crm_item_id]
    if with_signature:
        signature = tuple(tuple(getattr(task, field) for field in SOURCE_GRAPH_FIELDS) for task in tasks)
        return candidates, signature
    return candidates


def _lock_source_graph(reading_id, organization_id):
    candidates = lock_source_graphs([reading_id]).get(reading_id, [])
    if len(candidates) > 1:
        raise NeedConflict('Несколько исходных заявок: требуется отдельное разрешение связей.')
    if not candidates:
        return None
    task = candidates[0]
    if task.organization_id != organization_id:
        raise NeedConflict('Организация исходной заявки изменилась.')
    return task


def has_retained_source(reading_id):
    return bool(reading_id and (
        CrmObjectNeed.objects.filter(reading_id=reading_id).exists()
        or ServiceTask.objects.filter(water_reading_id=reading_id, task_type=ServiceTask.TYPE_SUPPLY_REQUEST).exists()))


@contextmanager
def source_write_context(reading):
    """Acquire order BEFORE Reading INSERT/UPDATE, not in post_save."""
    with transaction.atomic():
        if not reading.pk and not (reading.required_materials or '').strip():
            yield None, None, None
            return
        org_id = Pool.objects.filter(pk=reading.pool_id).values_list('organization_id', flat=True).first()
        if not org_id:
            # No org/task locks may be acquired after Reading SQL. Serialize a
            # legitimate unscoped save on its Pool, reject retained identities,
            # and never switch to a newly appeared organization in this branch.
            pool = Pool.objects.select_for_update().get(pk=reading.pool_id)
            if pool.organization_id or has_retained_source(reading.pk):
                raise NeedConflict('Организация исходной заявки отсутствует или изменилась.')
            reading.pool = pool
            yield None, None, None
            return
        Organization.objects.select_for_update().get(pk=org_id)
        task = _lock_source_graph(reading.pk, org_id) if reading.pk else None
        pool = Pool.objects.select_for_update().get(pk=reading.pool_id)
        client = Client.objects.select_for_update().get(pk=pool.client_id)
        pool.client = client
        reading.pool = pool
        old = WaterReading.objects.select_for_update().filter(pk=reading.pk).first() if reading.pk else None
        if old:
            old.pool = pool
        has_source = bool(task or (old and (old.required_materials or '').strip()) or (reading.required_materials or '').strip())
        if has_source and (pool.organization_id != org_id or client.organization_id != org_id):
            raise NeedConflict('Организация объекта изменилась.')
        # Reparenting a retained source identity is not a supported source write.
        if old and old.pool_id != reading.pool_id and (task or old.required_materials or reading.required_materials):
            raise NeedConflict('Объект исходной записи нельзя менять этим действием.')
        yield old, task, org_id


def _anchor_and_revision(reading, task, *, actor_id=None):
    from pool_service.services.task_generation import ensure_crm_for_supply_task
    if not task.crm_item_id:
        # Never add a new item to a frozen legacy task silently.
        if task.is_archived or task.completed_at or task.status not in ('new', 'in_progress'):
            raise NeedConflict('У завершённой исходной заявки нет однозначной CRM-связи.')
        ensure_crm_for_supply_task(task)
    validate_graph(reading, task, task.crm_item, task.organization_id)
    need = CrmObjectNeed.objects.select_for_update().filter(reading=reading).first()
    if need:
        if need.task_id != task.pk or need.crm_item_id != task.crm_item_id or need.organization_id != task.organization_id:
            raise NeedConflict('Каноническая связь исходной заявки изменилась.')
    else:
        need = CrmObjectNeed.objects.create(organization_id=task.organization_id, reading=reading,
            task=task, crm_item_id=task.crm_item_id)
    materials = (reading.required_materials or '').strip()
    source = need.sources.filter(sequence=need.source_revision).first()
    if need.source_revision and not source:
        raise NeedConflict('История редакций источника неполна. Требуется проверка.')
    if source and source.content_hash == materials_hash(materials):
        return need, source, False
    sequence = need.source_revision + 1
    source = CrmObjectNeedSourceRevision.objects.create(need=need, sequence=sequence,
        content_hash=materials_hash(materials), materials=materials, actor_id=actor_id)
    need.source_revision = sequence
    need.save()
    return need, source, True


def synchronize_source(reading, old=None, task=None, *, locked_org_id):
    """Called inside source_write_context after the source SQL succeeds."""
    from pool_service.services.task_generation import _create_supply_task_locked, _supply_description, _crm_supply_title
    materials = (reading.required_materials or '').strip()
    if locked_org_id is None:
        if has_retained_source(reading.pk) or (materials and reading.pool.organization_id is not None):
            raise NeedConflict('Источник изменился вне заранее заблокированной организации.')
        return None
    if reading.pool.organization_id != locked_org_id:
        raise NeedConflict('Организация исходной заявки изменилась.')
    if not task and not materials:
        return None
    if not task:
        task = _create_supply_task_locked(reading)
    need, source, changed = _anchor_and_revision(reading, task, actor_id=getattr(reading, '_source_actor_id', reading.added_by_id if old is None else None))
    if not changed or not old or not editable(task):
        return task
    previous = (old.required_materials or '').strip()
    # Update only fields which still equal their generated prior value. A
    # manually edited description/title/item is never overwritten by source.
    updates = []
    if task.auto_created and task.description == _supply_description(old):
        task.description = _supply_description(reading)
        updates.append('description')
    payload = dict(task.payload_json) if isinstance(task.payload_json, dict) else {}
    if task.auto_created and payload.get('required_materials') == previous:
        payload['required_materials'] = materials
        task.payload_json = payload
        updates.append('payload_json')
    if updates:
        task.save(update_fields=[*updates, 'updated_at'])
        ServiceTaskChange.objects.create(task=task, changed_by_id=getattr(reading, '_source_actor_id', reading.added_by_id),
            action=ServiceTaskChange.ACTION_UPDATED, field_name='source_revision', old_value=str(source.sequence - 1), new_value=str(source.sequence))
    item = task.crm_item
    item_fields = []
    for field, old_value, new_value in (
        ('title', _crm_supply_title(old), _crm_supply_title(reading)),
        ('description', _supply_description(old), _supply_description(reading)),
        ('equipment_replacement', previous, materials),
    ):
        if task.auto_created and getattr(item, field) == old_value and old_value != new_value:
            setattr(item, field, new_value)
            item_fields.append(field)
    if item_fields:
        item.save(update_fields=[*item_fields, 'updated_at'])
    return task


@transaction.atomic
def ensure_source(reading):
    with source_write_context(reading) as (old, task, locked_org_id):
        current = WaterReading.objects.select_related('pool__client', 'added_by').get(pk=reading.pk)
        return synchronize_source(current, old=old, task=task, locked_org_id=locked_org_id)


def source_tasks(user, organization):
    # SQL authority and full organization/object graph BEFORE search/counts.
    qs = ServiceTask.objects.filter(organization=organization, task_type=ServiceTask.TYPE_SUPPLY_REQUEST,
        water_reading__is_deleted=False, pool__is_deleted=False, water_reading__pool_id=F('pool_id'),
        pool__organization=organization, pool__client_id=F('client_id'), client__organization=organization).filter(
        Q(crm_item__isnull=True) | Q(crm_item__organization=organization, crm_item__pool_id=F('pool_id'),
        crm_item__client_id=F('client_id'), crm_item__direction=CrmItem.DIRECTION_SERVICE))
    # Supply sources have no valid call provenance. Reject malformed/injected
    # call payloads too; no publication route for a second private source.
    qs = qs.exclude(payload_json__has_key='source_call_id').filter(Q(payload_json__source__isnull=True) | ~Q(payload_json__source='call_analysis'))
    return readable_task_queryset(qs, user).select_related('pool__client', 'water_reading__added_by', 'crm_item', 'primary_responsible')


def authorize_review(task, actor, org):
    from pool_service.services.crm_commercial_commands import _authorize
    payload = task.payload_json if isinstance(task.payload_json, dict) else {}
    if 'source_call_id' in payload or payload.get('source') == 'call_analysis':
        raise PermissionDenied('Источник задачи недоступен для этого разбора.')
    from pool_service.services.permissions import company_has_access
    actor = _authorize('default', actor.pk, org, 'edit_own_deals')
    if not CrmAccess.objects.select_for_update().filter(organization=org, user_id=actor.pk, view_team_deals=True).exists():
        raise PermissionDenied('Индивидуальный доступ чтения CRM недоступен.')
    if not company_has_access(org) or task.primary_responsible_id != actor.pk or not editable(task):
        raise PermissionDenied('Разбор доступен только текущему ответственному своей редактируемой заявки.')
    # All memberships are held by _authorize. Task lock prevents concurrent
    # participant changes through the existing task edit flow.
    from pool_service.services.task_feedback_permissions import lock_feedback_actor
    lock_feedback_actor(task, actor)
    if not readable_tasks(source_tasks(actor, org).filter(pk=task.pk), actor):
        raise PermissionDenied('Исходная заявка недоступна.')
    return actor


def locked_task_version(task):
    data = snapshot(task)
    data['responsibles'] = sorted(task.responsibles.select_for_update().values_list('pk', flat=True))
    return digest(data)


@transaction.atomic
def review_need(*, actor, organization, task_id, source_revision, source_hash, review_revision, task_version, operation_key, positions):
    if type(task_id) is not int or task_id <= 0 or any(type(v) is not int or v < 0 for v in (source_revision, review_revision)):
        raise ValidationError('Некорректная версия исходной заявки.')
    if type(operation_key) is not str or len(operation_key) > 100 or not re.fullmatch(r'[a-z0-9][a-z0-9_-]*', operation_key):
        raise ValidationError('Некорректный ключ операции.')
    if type(source_hash) is not str or not re.fullmatch('[0-9a-f]{64}', source_hash) or type(task_version) is not str or not re.fullmatch('[0-9a-f]{64}', task_version):
        raise ValidationError('Некорректная версия состава/задачи.')
    validate_positions(positions)
    positions = [dict(line) for line in positions]
    validate_positions(positions)
    payload_hash = digest(dict(actor=actor.pk, organization=organization.pk, task_id=task_id,
        source_revision=source_revision, source_hash=source_hash, review_revision=review_revision,
        task_version=task_version, positions=positions, command_type='object_need.review'))
    org = Organization.objects.select_for_update().get(pk=organization.pk)
    seed = ServiceTask.objects.filter(pk=task_id, organization=org).values('water_reading_id').first()
    if not seed or not seed['water_reading_id']:
        raise PermissionDenied
    task = _lock_source_graph(seed['water_reading_id'], org.pk)
    if not task or task.pk != task_id:
        raise PermissionDenied
    actor = authorize_review(task, actor, org)
    pool = Pool.objects.select_for_update().get(pk=task.pool_id)
    client = Client.objects.select_for_update().get(pk=task.client_id)
    reading = WaterReading.objects.select_for_update().get(pk=task.water_reading_id)
    pool.client = client
    reading.pool = pool
    validate_graph(reading, task, task.crm_item, org.pk)
    if reading.is_deleted or pool.is_deleted:
        raise PermissionDenied
    # Repeat source privacy after the current locked source graph is loaded.
    if not readable_tasks(source_tasks(actor, org).filter(pk=task.pk), actor):
        raise PermissionDenied
    need = CrmObjectNeed.objects.select_for_update().filter(reading=reading).first()
    current = need.sources.select_for_update().filter(sequence=need.source_revision).first() if need else None
    current_revision = need.source_revision if need else 0
    current_review = need.review_revision if need else 0
    materials = (reading.required_materials or '').strip()
    if len(materials) > 20000:
        raise ValidationError('Состав превышает предел экрана разбора. Проверьте исходную задачу.')
    current_hash = materials_hash(materials)
    if current and current.content_hash != current_hash:
        raise NeedConflict('Состав источника изменился вне поддерживаемого writer. Требуется проверка.')
    command = CrmCommand.objects.filter(organization=org, operation_key=operation_key).first()
    if command:
        if command.payload_hash != payload_hash:
            raise NeedConflict('Ключ операции уже использован для другого разбора.')
        if (current_revision != command.result.get('source_revision') or current_hash != source_hash
                or locked_task_version(task) != task_version):
            raise NeedConflict('После разбора изменился состав или задача исходной записи.')
        return dict(command.result)
    if (current_revision != source_revision or current_review != review_revision
            or current_hash != source_hash or locked_task_version(task) != task_version):
        raise NeedConflict('Состав, задача или разбор изменились. Обновите данные и проверьте позиции.')
    need, source, _ = _anchor_and_revision(reading, task, actor_id=reading.added_by_id)
    sequence = need.review_revision + 1
    command = CrmCommand.objects.create(organization=org, operation_key=operation_key,
        actor=actor, command_type='object_need.review', payload_hash=payload_hash)
    review = CrmObjectNeedReview.objects.create(need=need, source=source, sequence=sequence,
        actor=actor, command=command, positions=positions)
    need.review_revision = sequence
    need.save()
    command.result = {'need_id': need.pk, 'review_id': review.pk, 'revision': sequence, 'source_revision': source.sequence}
    command.save(update_fields=['result'])
    return dict(command.result)
