"""Explicit selected-line identity and own commercial conversion.

All supported writes join the Organization mutex and acquire the complete
existing task set before actor locks. No operational source is written here.
"""
from dataclasses import dataclass
import re

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import router, transaction
from django.db.models import Max, Min, Q, Subquery

from pool_service.crm_commercial_models import CrmAccess, CrmCommand, CrmDeal
from pool_service.crm_object_need_models import (
    CrmObjectNeed, CrmObjectNeedReview, CrmObjectNeedSourceRevision,
    CrmObjectNeedLine, CrmObjectNeedReviewLine, CrmObjectNeedDealLine,
)
from pool_service.models import Client, CrmItem, Organization, Pool, ServiceTask, WaterReading
from .crm_commercial_commands import _authorize, _positive_id
from .crm_deal_objects import object_querysets
from .crm_object_needs import (
    NeedConflict, authorize_review, digest, locked_task_version, materials_hash,
    source_graph_queryset, source_graph_signature, source_tasks, validate_graph,
)
from .task_read_permissions import readable_tasks


def _key(value):
    if type(value) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,99}', value):
        raise ValidationError('Некорректный ключ операции.')


def _selected(values, *, reconcile=False):
    fields = {'line_token', 'kind', 'line_id'} if reconcile else {'line_token', 'line_id'}
    if type(values) is not list or not 1 <= len(values) <= 50:
        raise ValidationError('Выберите от 1 до 50 согласуемых позиций.')
    tokens, identities, result = set(), set(), []
    for row in values:
        if type(row) is not dict or set(row) != fields:
            raise ValidationError('Некорректные поля выбранной позиции.')
        token = row['line_token']
        if type(token) is not str or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', token) or token in tokens:
            raise ValidationError('Выберите уникальные идентификаторы строк.')
        line_id = row['line_id']
        if reconcile and row['kind'] == 'new':
            if line_id is not None:
                raise ValidationError('Идентификатор новой позиции выдаёт сервер.')
        else:
            if reconcile and row['kind'] != 'existing':
                raise ValidationError('Явно выберите прежнюю или новую самостоятельную потребность.')
            _positive_id(line_id)
            if line_id in identities:
                raise ValidationError('Одна самостоятельная позиция не может быть выбрана дважды.')
            identities.add(line_id)
        tokens.add(token)
        result.append(dict(row))
    return sorted(result, key=lambda row: row['line_token'])


@dataclass(frozen=True)
class NeedDealContext:
    """Internal certificate bound to the outer transaction, never HTTP input."""
    payload: dict
    actor_id: int
    organization_id: int
    client_id: int
    pool_id: int
    using: str
    atomic_scope: object
    task_ids: frozenset

    def validate(self, *, actor_id, organization_id, using, previous=None):
        connection = transaction.get_connection(using)
        if (self.using != using or self.actor_id != actor_id or self.organization_id != organization_id
                or not connection.in_atomic_block or self.atomic_scope not in connection.atomic_blocks
                or (previous and previous.result.get('task_id') not in self.task_ids)):
            raise PermissionDenied('Контекст исходной потребности недоступен.')


def _lock_tasks(reading_id, extra_ids, using):
    graph = source_graph_queryset([reading_id])
    rows = list(ServiceTask.objects.using(using).select_for_update().filter(
        Q(pk__in=Subquery(graph.values('pk'))) | Q(pk__in=extra_ids)).order_by('pk'))
    items = {item.pk: item for item in CrmItem.objects.using(using).select_for_update().filter(
        pk__in={row.crm_item_id for row in rows if row.crm_item_id}).order_by('pk')}
    for row in rows:
        if row.crm_item_id:
            row.crm_item = items[row.crm_item_id]
    candidates = [row for row in rows if row.water_reading_id == reading_id and row.task_type == 'supply_request']
    if len(candidates) != 1:
        raise NeedConflict('Исходная заявка неоднозначна. Проверьте связи.')
    signature = source_graph_signature([reading_id])
    task_ids = frozenset(row.pk for row in rows)
    if any(row[0] not in task_ids for row in signature):
        raise NeedConflict('Связи исходной заявки выросли. Обновите данные.')
    return candidates[0], task_ids, signature


def _locked_source(*, actor, organization, task_id, source_revision, source_hash,
                   review_id, review_revision, task_version, operation_key, using):
    for value in (task_id, source_revision, review_id, review_revision):
        _positive_id(value)
    for value in (source_hash, task_version):
        if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
            raise ValidationError('Некорректная версия источника.')
    org = Organization.objects.using(using).select_for_update().get(pk=organization.pk)
    previous = CrmCommand.objects.using(using).select_for_update().filter(organization=org, operation_key=operation_key).first()
    seed = CrmObjectNeed.objects.using(using).filter(organization=org, task_id=task_id).values('reading_id').first()
    if not seed:
        raise PermissionDenied('Исходная потребность недоступна.')
    extra_ids = [previous.result.get('task_id')] if previous else []
    task, task_ids, signature = _lock_tasks(seed['reading_id'], extra_ids, using)
    if task.pk != task_id or task.organization_id != org.pk:
        raise PermissionDenied
    actor = authorize_review(task, actor, org)
    actor = _authorize(using, actor.pk, org, ('create_own_deals', 'manage_own_next_action'))
    pool = Pool.objects.using(using).select_for_update().get(pk=task.pool_id)
    client = Client.objects.using(using).select_for_update().get(pk=task.client_id)
    reading = WaterReading.objects.using(using).select_for_update().get(pk=task.water_reading_id)
    pool.client = client
    reading.pool = pool
    validate_graph(reading, task, task.crm_item, org.pk)
    from pool_service.client_queries import active_clients
    pools, _ = object_querysets(actor, org.pk, using=using, lock=True)
    if (reading.is_deleted or pool.is_deleted
            or not active_clients(Client.objects.using(using).select_for_update()).filter(pk=client.pk).exists()
            or not pools.filter(pk=pool.pk, client_id=client.pk).exists()
            or not readable_tasks(source_tasks(actor, org).filter(pk=task.pk), actor)):
        raise PermissionDenied('Исходный объект или заявка сейчас недоступны.')
    if source_graph_signature([reading.pk]) != signature:
        raise NeedConflict('Связи исходной заявки изменились. Обновите данные.')
    need = CrmObjectNeed.objects.using(using).select_for_update().get(reading_id=reading.pk)
    if need.task_id != task.pk or need.crm_item_id != task.crm_item_id or need.organization_id != org.pk:
        raise NeedConflict('Каноническая связь потребности изменилась.')
    source = CrmObjectNeedSourceRevision.objects.using(using).select_for_update().filter(need=need, sequence=need.source_revision).first()
    # A committed exact replay may refer to an older review of the still-current
    # source. Current rights/graph/source/task checks remain mandatory; the
    # caller verifies the original command hash before returning any result.
    # A new operation always uses the newest review.
    review_sequence = review_revision if previous else need.review_revision
    review = CrmObjectNeedReview.objects.using(using).select_for_update().filter(need=need, sequence=review_sequence).first()
    materials = (reading.required_materials or '').strip()
    if (not source or not review or len(materials) > 20000
            or source.sequence != source_revision or source.content_hash != source_hash
            or source.content_hash != materials_hash(materials)
            or review.pk != review_id or review.sequence != review_revision or review.source_id != source.pk
            or locked_task_version(task) != task_version):
        raise NeedConflict('Состав, разбор или задача изменились. Обновите данные.')
    return org, actor, need, review, pool, client, previous, task_ids


def _positions(review, selected):
    positions = {row['line_token']: row for row in review.positions}
    if any(row['line_token'] not in positions or positions[row['line_token']]['mode'] != 'approval_required' for row in selected):
        raise ValidationError('Выбирать можно только текущие позиции «Требуется согласование».')


def reconcile_need_positions(*, actor, organization, operation_key, selections, **source):
    _key(operation_key)
    selected = _selected(selections, reconcile=True)
    payload_hash = digest(dict(kind='object_need.reconcile', actor_id=actor.pk,
        organization_id=organization.pk, selections=selected, **source))
    using = router.db_for_write(CrmCommand)
    with transaction.atomic(using=using):
        org, actor, need, review, _pool, _client, previous, _tasks = _locked_source(
            actor=actor, organization=organization, operation_key=operation_key, using=using, **source)
        _positions(review, selected)
        if previous:
            if previous.actor_id != actor.pk or previous.command_type != 'object_need.reconcile' or previous.payload_hash != payload_hash:
                raise NeedConflict('Ключ операции уже использован с другим содержимым.')
            return dict(previous.result)
        tokens = [row['line_token'] for row in selected]
        # The Organization mutex serializes supported immutable binding writers.
        # Aggregate in SQL: at most 50 rows cross into Python, regardless of
        # review history length. MIN/MAX also detects inconsistent old aliases.
        known = {row['line_token']: row for row in
            CrmObjectNeedReviewLine.objects.using(using).filter(need=need, line_token__in=tokens)
            .order_by().values('line_token').annotate(first_line=Min('line_id'), last_line=Max('line_id'))}
        current = {binding.line_token: binding for binding in
            CrmObjectNeedReviewLine.objects.using(using).select_for_update().filter(
                need=need, review=review, line_token__in=tokens).order_by('pk')}
        lines = {line.pk: line for line in CrmObjectNeedLine.objects.using(using).select_for_update().filter(
            need=need, pk__in=[row['line_id'] for row in selected if row['line_id']]).order_by('pk')}
        command = CrmCommand.objects.using(using).create(organization=org, actor=actor,
            operation_key=operation_key, command_type='object_need.reconcile', payload_hash=payload_hash)
        for row in selected:
            prior = known.get(row['line_token'])
            existing = current.get(row['line_token'])
            if prior and (prior['first_line'] != prior['last_line']
                    or row['kind'] == 'new' or row['line_id'] != prior['first_line']):
                raise NeedConflict('Известная строка уже имеет устойчивую позицию. Выберите её прежний ID.')
            if existing:
                if existing.line_id != row['line_id']:
                    raise NeedConflict('Сопоставление позиции уже сохранено.')
                continue
            if row['kind'] == 'new':
                line = CrmObjectNeedLine.objects.using(using).create(need=need, creation_command=command, created_by=actor)
            else:
                line = lines.get(row['line_id'])
                if not line:
                    raise PermissionDenied('Прежняя позиция недоступна в этой потребности.')
            if CrmObjectNeedReviewLine.objects.using(using).filter(review=review, line=line).exists():
                raise NeedConflict('Эта самостоятельная позиция уже сопоставлена другой строке разбора.')
            CrmObjectNeedReviewLine.objects.using(using).create(need=need, review=review,
                line_token=row['line_token'], line=line, command=command, resolution_kind=row['kind'], created_by=actor)
        command.result = dict(need_id=need.pk, review_id=review.pk, source_revision=review.source.sequence)
        command.save(using=using, update_fields=['result'])
        return dict(command.result)


def convert_need_positions_to_deal(*, actor, organization, operation_key, selections,
                                   client_id, pipeline_id, title, next_action, **source):
    from .crm_commercial_commands import create_commercial_deal
    _key(operation_key)
    selected = _selected(selections)
    using = router.db_for_write(CrmCommand)
    with transaction.atomic(using=using):
        scope = transaction.get_connection(using).atomic_blocks[-1]
        org, actor, need, review, pool, client, previous, tasks = _locked_source(
            actor=actor, organization=organization, operation_key=operation_key, using=using, **source)
        _positive_id(client_id)
        if client_id != client.pk:
            raise PermissionDenied('Клиент должен совпадать с исходным объектом.')
        _positions(review, selected)
        bindings = {row.line_token: row for row in CrmObjectNeedReviewLine.objects.using(using).select_for_update().filter(
            need=need, review=review, line_token__in=[row['line_token'] for row in selected]).order_by('pk')}
        for row in selected:
            binding = bindings.get(row['line_token'])
            if not binding or binding.line_id != row['line_id']:
                raise NeedConflict('Сначала явно сопоставьте выбранные позиции с устойчивыми ID.')
        list(CrmObjectNeedLine.objects.using(using).select_for_update().filter(
            need=need, pk__in=[row['line_id'] for row in selected]).order_by('pk'))
        claims = list(CrmObjectNeedDealLine.objects.using(using).select_for_update().filter(
            line_id__in=[row['line_id'] for row in selected]).order_by('pk'))
        if any(not previous or claim.command_id != previous.pk for claim in claims):
            raise NeedConflict('Выбранная позиция уже связана со сделкой. Новая сделка не создана.')
        context_payload = dict(kind='object_need.convert', need_id=need.pk, selections=selected, **source)
        context = NeedDealContext(context_payload, actor.pk, org.pk, client.pk, pool.pk, using, scope, tasks)
        result = create_commercial_deal(actor=actor, organization=org, operation_key=operation_key,
            client_id=client.pk, pipeline_id=pipeline_id, title=title, next_action=next_action,
            card_fields={'object_kind': 'pool', 'pool_id': pool.pk}, source_context=context)
        command = CrmCommand.objects.using(using).get(organization=org, operation_key=operation_key)
        if previous:
            if len(claims) != len(selected) or {claim.review_line_id for claim in claims} != {row.pk for row in bindings.values()}:
                raise NeedConflict('История конвертации неполна. Требуется проверка.')
            return result
        command.result = dict(result, need_id=need.pk, review_id=review.pk, source_revision=review.source.sequence)
        command.save(using=using, update_fields=['result'])
        deal = CrmDeal.objects.using(using).get(pk=result['deal_id'])
        for row in selected:
            binding = bindings[row['line_token']]
            CrmObjectNeedDealLine.objects.using(using).create(line_id=binding.line_id,
                review_line=binding, deal=deal, command=command, created_by=actor)
        return dict(command.result)


def readable_need(actor, organization, task_id):
    """Independent current read boundary, including object access, never own-write."""
    from .crm_commercial_access import can_view_team_deals
    from .permissions import company_has_access
    if (not company_has_access(organization) or not can_view_team_deals(actor, organization)
            or not CrmAccess.objects.filter(organization=organization, user_id=actor.pk, view_team_deals=True).exists()):
        raise PermissionDenied
    tasks = readable_tasks(source_tasks(actor, organization).filter(pk=task_id), actor)
    if len(tasks) != 1:
        raise PermissionDenied
    task = tasks[0]
    pools, _ = object_querysets(actor, organization.pk)
    if not pools.filter(pk=task.pool_id, client_id=task.client_id).exists():
        raise PermissionDenied
    need = CrmObjectNeed.objects.filter(task=task, organization=organization,
        reading_id=task.water_reading_id, crm_item_id=task.crm_item_id).first()
    if not need:
        raise PermissionDenied
    validate_graph(task.water_reading, task, task.crm_item, organization.pk)
    return need


def visible_deal_link(deal, actor, organization):
    from django.urls import reverse
    from pool_service.crm_live_views import _deal_queryset
    if not _deal_queryset(actor, organization).filter(pk=deal.pk).exists():
        return None
    return {'id': deal.pk, 'title': deal.title,
        'url': reverse('crm_live_workspace') + f'?org_id={organization.pk}&pipeline={deal.pipeline_id}&deal={deal.pk}'}


def retained_lines(actor, organization, task_id, *, query='', page=1):
    """Scope before search/count; bounded choices include all retained pages."""
    from django.core.paginator import Paginator
    need = readable_need(actor, organization, task_id)
    qs = CrmObjectNeedLine.objects.filter(need=need).select_related('conversion__deal')
    query = query.strip()[:64]
    if query:
        predicate = Q(bindings__line_token__icontains=query)
        if query.isascii() and query.isdigit() and len(query) <= 18:
            predicate |= Q(pk=int(query))
        qs = qs.filter(predicate).distinct()
    batch = Paginator(qs.order_by('-pk'), 50).get_page(page)
    rows = []
    for line in batch:
        binding = line.bindings.select_related('review__source').order_by('pk').first()
        position = next((p for p in binding.review.positions if p['line_token'] == binding.line_token), None) if binding else None
        claim = getattr(line, 'conversion', None)
        rows.append({'id': line.pk, 'description': position['description'] if position else '',
            'source_revision': binding.review.source.sequence if binding else None,
            'consumed': bool(claim), 'deal': visible_deal_link(claim.deal, actor, organization) if claim else None})
    return dict(rows=rows, page=batch.number, pages=batch.paginator.num_pages, total=batch.paginator.count)


def current_line_bindings(actor, organization, task_id, review):
    need = readable_need(actor, organization, task_id)
    if not review or review.need_id != need.pk or review.source.sequence != need.source_revision:
        return []
    rows = CrmObjectNeedReviewLine.objects.filter(need=need, review=review).select_related('line__conversion__deal').order_by('pk')[:50]
    result = []
    for binding in rows:
        claim = getattr(binding.line, 'conversion', None)
        result.append({'line_token': binding.line_token, 'line_id': binding.line_id,
            'consumed': bool(claim), 'deal': visible_deal_link(claim.deal, actor, organization) if claim else None})
    return result


def deal_need_provenance(deal, actor, organization):
    """Only detail asks for provenance; team cards never join private text."""
    from django.urls import reverse
    claims = list(CrmObjectNeedDealLine.objects.filter(deal=deal).select_related(
        'line__need', 'review_line__review__source').order_by('pk')[:51])
    if not claims:
        return None
    if len(claims) > 50 or len({claim.line.need_id for claim in claims}) != 1:
        return {'restricted': True}
    try:
        need = readable_need(actor, organization, claims[0].line.need.task_id)
        if any(claim.line.need_id != need.pk or claim.review_line.review.need_id != need.pk for claim in claims):
            raise PermissionDenied
    except (PermissionDenied, ValidationError):
        return {'restricted': True}
    rows = []
    for claim in claims:
        position = next((p for p in claim.review_line.review.positions if p['line_token'] == claim.review_line.line_token), None)
        if not position:
            return {'restricted': True}
        rows.append({'line_id': claim.line_id, 'description': position['description'],
            'reason': position['reason'], 'review_revision': claim.review_line.review.sequence,
            'source_revision': claim.review_line.review.source.sequence})
    return {'url': reverse('crm_object_needs') + f'?org_id={organization.pk}&need_task={need.task_id}',
        'task_url': reverse('task_edit', args=[need.task_id]), 'positions': rows}
