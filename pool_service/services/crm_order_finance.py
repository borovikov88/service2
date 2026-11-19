"""Explicit order links and projections from complete, saved 1C evidence.

No network on reads and no inference of payment from a balance or a forecast.
Only the server reader supplies snapshots; HTTP accepts an internal order ID.
"""
from pool_service.services.crm_role_runtime import resolve_crm_capabilities
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from uuid import UUID

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.client_crm_models import ClientCRMProfile
from pool_service.client_queries import active_clients
from pool_service.crm_commercial_models import CANONICAL_KEY, CrmAccess, CrmCommand, CrmDeal, CrmEvent, CrmStage
from pool_service.crm_order_models import CrmDealDocumentLink, CrmDealOrderLink, CrmOneCOrder, CrmOrderEvidenceSnapshot, CrmOrderSyncState, CrmOrderLinkEvent
from pool_service.models import Client, Employee, Organization, OrganizationAccess, ServiceTask
from pool_service.reward_models import OneCCustomerIdentity
from .crm_commercial_access import can_view_team_deals
from .crm_commercial_commands import CommercialConflict, _authorize, _owned_deal, _positive_id
from .finance import can_access_finance_overview
from .permissions import company_has_access


def _json(value):
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _finance_actor(actor, organization, *, lock=False):
    queryset = get_user_model().objects
    if lock:
        queryset = queryset.select_for_update()
        list(OrganizationAccess.objects.select_for_update().filter(user_id=actor.pk, organization=organization))
        list(Employee.objects.select_for_update().filter(user_id=actor.pk, organization=organization))
    current = queryset.filter(pk=getattr(actor, 'pk', None), is_active=True).first()
    if (not current or not can_access_finance_overview(current, organization)
            or Employee.objects.filter(user_id=current.pk, organization=organization).filter(
                Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED)).exists()):
        raise PermissionDenied('Данные заказа 1С недоступны при текущих правах.')
    return current


def _object_scope(deal):
    return {'kind': deal.object_kind, 'pool': deal.pool_id, 'client_object': deal.client_object_id,
            'planned_name': deal.planned_object_name, 'planned_address': deal.planned_object_address}


def _order_scope(order):
    return _hash([order.source_id, order.external_organization, order.order_guid, order.customer_guid,
                  order.agreement_guid, order.currency_guid, order.currency, str(order.amount)])


def _confirmed_customers(organization, client_id, *, lock=False):
    if not active_clients(Client.objects.filter(organization=organization)).filter(pk=client_id).exists():
        return set()
    identities = OneCCustomerIdentity.objects.filter(organization=organization, client_id=client_id,
        client__organization=organization, confirmed_at__isnull=False, confirmed_by__isnull=False,
        confirmed_at__lte=timezone.now())
    profiles = ClientCRMProfile.objects.filter(client_id=client_id, client__organization=organization,
        source__in=[ClientCRMProfile.SOURCE_ONEC, ClientCRMProfile.SOURCE_ONEC_IP],
        merged_into__isnull=True, last_synced_at__isnull=False, last_synced_at__lte=timezone.now())
    if lock:
        identities = identities.select_for_update()
        profiles = profiles.select_for_update()
    values = list(identities.values_list('onec_customer_id', flat=True))
    # A verified imported profile is an existing authoritative 1C identity too.
    values.extend(profiles.values_list('onec_ref', flat=True))
    result = set()
    for value in values:
        try:
            guid = UUID(str(value))
            if guid.int:
                result.add(str(guid))
        except (TypeError, ValueError, AttributeError):
            continue
    return result


def _recurring_required(deal):
    from pool_service.crm_recurring_models import CrmRecurringConfirmation
    return CrmRecurringConfirmation.objects.filter(organization_id=deal.organization_id,
        revision__draft__deal_id=deal.pk).exists()


def _service_contract_problem(deal, order):
    from pool_service.crm_service_binding_models import ServiceContractBinding
    binding = ServiceContractBinding.objects.filter(deal=deal).first()
    if not binding:
        return ''
    try:
        from pool_service.finance_imports.odata_finance_position import config_from_settings
        from pool_service.finance_imports.odata_crm_orders import source_identity
        config = config_from_settings()
        source_matches = binding.source_id == _hash(config.base_url) and order.source_id == source_identity(config)
    except Exception:
        source_matches = False
    if (not source_matches or binding.organization_id != deal.organization_id
            or binding.client_id != deal.client_id or binding.external_organization != order.external_organization
            or binding.external_client != order.customer_guid or binding.contract_ref != order.agreement_guid
            or binding.object_kind != deal.object_kind or binding.pool_id != deal.pool_id
            or binding.client_object_id != deal.client_object_id):
        return 'Заказ не соответствует подтверждённому договору и объекту обслуживания.'
    return ''


def current_order_source(organization_id):
    if str(getattr(settings, 'ONEC_ODATA_TARGET_ORGANIZATION_ID', '')) != str(organization_id):
        return None
    try:
        from pool_service.finance_imports.odata_crm_orders import source_identity
        from pool_service.finance_imports.odata_finance_position import config_from_settings
        from pool_service.finance_imports.odata_profit import validate_config
        config = validate_config(config_from_settings())
        return config, source_identity(config)
    except Exception:
        return None


def _in_current_source(row, source):
    return bool(source and row.source_id == source[1] and row.external_organization in source[0].organization_guids)


def _fresh(order, state=None, now=None):
    now = now or timezone.now()
    state = state or CrmOrderSyncState.objects.filter(organization_id=order.organization_id).first()
    age = timedelta(hours=max(1, min(72, int(getattr(settings, 'ONEC_CRM_ORDER_MAX_AGE_HOURS', 30)))))
    return bool(_in_current_source(order, current_order_source(order.organization_id))
        and state and not state.failed and state.source_id == order.source_id and not order.missing and not order.lookup_failed
        and now - age <= order.source_at <= order.fetched_at <= now + timedelta(minutes=1))


def _link_problem(link, deal):
    order = link.order
    if (link.organization_id != deal.organization_id or order.organization_id != deal.organization_id
            or deal.client_id != link.client_id or _object_scope(deal) != link.object_scope
            or order.customer_guid not in _confirmed_customers(deal.organization, deal.client_id)
            or _order_scope(order) != link.scope_fingerprint):
        return 'Клиент, объект или условия заказа изменились. Подтвердите связь заново.'
    contract_problem = _service_contract_problem(deal, order)
    if contract_problem:
        return contract_problem
    if deal.currency and deal.currency != order.currency:
        return 'Валюта сделки отличается от валюты заказа. Требуется сверка.'
    if link.requires_reconciliation:
        return link.review_reason or 'Изменились данные завершённой сделки. Требуется сверка.'
    if order.missing:
        return 'Актуальность заказа не подтверждена. Обновите данные 1С.'
    if not order.order_valid or order.cancelled:
        return 'Заказ отменён или удалён в текущем источнике. Требуется сверка.'
    return ''


def _money(value):
    return format(value, '.2f') if value is not None else None


def _percent(value, amount):
    if value is None or amount is None or amount <= 0:
        return None
    return format((max(Decimal(0), value) / amount * 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f')


def _order_data(order):
    return {'id': order.pk, 'number': order.number, 'date': order.order_date.isoformat(),
        'amount': _money(order.amount), 'currency': order.currency,
        'state': 'Отменён' if order.cancelled else 'Подтверждён в 1С' if order.order_valid else 'Требуется сверка'}


def _writable(user, organization, deal):
    grant = resolve_crm_capabilities(user.pk, organization.pk).edit_own_deals
    return bool(organization.crm_v2_write and company_has_access(organization) and grant
        and deal.manager_id == user.pk and deal.outcome == 'open' and not deal.is_archived)


def _open_action(deal):
    pointer = getattr(deal, 'next_action', None)
    if not pointer:
        return False
    task = pointer.task_link.task
    return not (task.is_archived or task.completed_at or task.status in {ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED})


def finance_projection(deal, actor, organization, *, now=None):
    base = {'state': 'restricted', 'can_bind': False, 'can_unbind': False, 'order': None,
        'realized_amount': None, 'paid_amount': None, 'completion_percent': None, 'payment_percent': None,
        'source_at': None, 'reason': 'Данные 1С доступны сотрудникам с финансовыми правами.',
        'recurring': deal.pipeline.code == 'service' or deal.fulfillment_kind == 'service' or _recurring_required(deal),
        'auto_complete': False,
        'completed_automatically': False, 'open_action_preserved': False,
        'period_label': 'По привязанному заказу', 'documents': []}
    try:
        user = _finance_actor(actor, organization)
    except PermissionDenied:
        return base
    if deal.organization_id != organization.pk or not can_view_team_deals(user, organization):
        return base
    writable = _writable(user, organization, deal)
    base.update(state='unlinked', can_bind=writable,
        reason='Привяжите заказ клиента, чтобы видеть выполнение и оплату из 1С.')
    link = CrmDealOrderLink.objects.filter(organization=organization, deal=deal).select_related('order').first()
    if not link:
        return base
    order = link.order
    base.update(can_unbind=writable, recurring=link.recurring or _recurring_required(deal),
        completed_automatically=link.completed_automatically,
        open_action_preserved=link.completed_automatically and _open_action(deal))
    if not _in_current_source(order, current_order_source(organization.pk)):
        base.update(state='review' if order.missing else 'stale', reason='Источник связанного заказа изменился. Требуется сверка.')
        return base
    problem = _link_problem(link, deal)
    if problem:
        # A changed party must not disclose the replacement customer's order.
        base.update(state='review', reason=problem)
        return base
    base.update(order=_order_data(order), source_at=order.source_at.isoformat())
    if not _fresh(order, now=now):
        base.update(state='stale', reason='Данные 1С требуют обновления. Автозавершение приостановлено.')
        return base
    base.update(state='current', reason='',
        realized_amount=_money(order.realized_amount) if order.realization_complete else None,
        paid_amount=_money(order.paid_amount) if order.payment_complete else None,
        completion_percent=_percent(order.realized_amount, order.amount) if order.realization_complete else None,
        payment_percent=_percent(order.paid_amount, order.amount) if order.payment_complete else None,
        documents=[{key: doc.get(key) for key in ('number', 'date', 'amount', 'paid_amount')}
                   for doc in order.documents if doc.get('kind') == 'realization'])
    if not order.currency:
        base.update(state='review', reason='Валюта заказа не сопоставлена. Проценты рассчитаны в валюте заказа; автозавершение приостановлено.')
    elif order.blockers or not order.realization_complete or not order.payment_complete:
        base.update(state='review', reason='Часть документов требует сверки. Неподтверждённый показатель не рассчитывается.')
    elif deal.outcome in {'lost', 'cancelled'}:
        base['reason'] = 'Сделка завершена без продажи. Связь с заказом и подтверждённые документы 1С сохранены.'
    elif base['recurring']:
        base['reason'] = 'Постоянное обслуживание остаётся открытым после выполнения и оплаты заказа.'
    elif link.completed_automatically:
        base['reason'] = 'Сделка автоматически завершена по подтверждённым реализации и оплате заказа.'
    else:
        base['reason'] = 'Разовая сделка завершится автоматически после полной реализации и оплаты заказа.'
    base['auto_complete'] = bool(deal.outcome == 'open' and not deal.is_archived
        and not base['recurring'] and order.currency and not order.blockers
        and order.realization_complete and order.payment_complete)
    return base


def order_candidates(*, actor, organization, deal, query='', exact=False):
    user = _finance_actor(actor, organization)
    if not can_view_team_deals(user, organization) or not _writable(user, organization, deal):
        raise PermissionDenied
    customers = _confirmed_customers(organization, deal.client_id)
    if not customers:
        return {'orders': [], 'reason': 'У клиента нет подтверждённой связи с 1С. Сначала свяжите карточку клиента.'}
    source = current_order_source(organization.pk)
    if not source:
        return {'orders': [], 'reason': 'Источник документов 1С недоступен.'}
    config, source_id = source
    state = CrmOrderSyncState.objects.filter(organization=organization, source_id=source_id).first()
    rows = CrmOneCOrder.objects.filter(organization=organization, source_id=source_id, external_organization__in=config.organization_guids,
        customer_guid__in=customers, order_valid=True, cancelled=False, missing=False).filter(
            Q(deal_link__isnull=True) | Q(deal_link__deal_id=deal.pk)).filter(
            Q(document_claim__isnull=True) | Q(document_claim__deal_id=deal.pk))
    if query:
        rows = rows.filter(**({'number': query} if exact else {'number__icontains': query[:100]}))
    items = []
    for row in rows.order_by('-order_date', '-pk')[:50]:
        current = bool(state and _fresh(row, state))
        items.append({**_order_data(row), 'source_at': row.fetched_at.isoformat(), 'financially_current': current,
            'state': 'Подтверждён в 1С' if current else 'Последняя сохранённая версия'})
    return {'orders': items, 'reason': '' if items else 'В сохранённых данных заказы не найдены. Можно проверить точный номер в 1С.'}


class OrderRefreshUnavailable(Exception):
    """A safe boundary for transport errors; never render upstream text."""


def _order_lookup_scope(actor, organization, deal_id, *, operational=False):
    """The caller holds a short transaction, never a network call under locks."""
    organization = Organization.objects.select_for_update().get(pk=organization.pk)
    user = _authorize('default', actor.pk, organization, 'edit_own_deals')
    if not operational:
        user = _finance_actor(user, organization, lock=True)
    deal = _owned_deal('default', user.pk, organization.pk, deal_id)
    if not _writable(user, organization, deal):
        raise PermissionDenied
    customers = tuple(sorted(_confirmed_customers(organization, deal.client_id, lock=True)))
    if not customers:
        raise ValidationError('У клиента нет подтверждённой связи с 1С. Сначала свяжите карточку клиента.')
    return user, organization, deal, customers


def _invalidate_failed_order_lookup(*, actor, organization, deal_id, pinned, required, config, source_id, started_at, operational):
    if not required:
        return
    with transaction.atomic():
        user, organization, deal, customers = _order_lookup_scope(actor, organization, deal_id, operational=operational)
        if pinned != (deal.client_id, _object_scope(deal), customers):
            raise CommercialConflict('Клиент или объект сделки изменился. Повторите поиск.')
        # A failed recheck is not evidence of cancellation or a replacement
        # customer. Preserve all facts and only withdraw their current status.
        # Do not invalidate a successful newer observation that won the race.
        CrmOneCOrder.objects.filter(organization=organization, source_id=source_id,
            external_organization__in=config.organization_guids, customer_guid__in=customers,
            order_guid__in=required, fetched_at__lte=started_at).update(lookup_failed=True)


def refresh_order_candidates(*, actor, organization, deal_id, query, clock=None, operational=False):
    """Explicit exact-number lookup, scoped to the deal's confirmed customers."""
    from pool_service.finance_imports.odata_crm_orders import read_crm_orders, source_identity
    from pool_service.finance_imports.odata_finance_position import config_from_settings
    from pool_service.finance_imports.odata_profit import validate_config
    _positive_id(deal_id)
    if type(query) is not str or not 1 <= len(query.strip()) <= 80 or any(ord(char) < 32 for char in query):
        raise ValidationError('Укажите точный номер заказа 1С (до 80 символов).')
    query = query.strip()
    clock = clock or timezone.now
    with transaction.atomic():
        user, organization, deal, customers = _order_lookup_scope(actor, organization, deal_id, operational=operational)
        pinned = (deal.client_id, _object_scope(deal), customers)
        try:
            config = validate_config(config_from_settings())
            source_id = source_identity(config)
        except Exception as exc:
            raise OrderRefreshUnavailable from exc
        if str(getattr(settings, 'ONEC_ODATA_TARGET_ORGANIZATION_ID', '')) != str(organization.pk):
            raise PermissionDenied
        required = tuple(sorted(set(CrmOneCOrder.objects.filter(organization=organization,
            source_id=source_id, external_organization__in=config.organization_guids,
            customer_guid__in=customers, number=query).values_list('order_guid', flat=True))))
    started_at = clock()
    try:
        snapshot = read_crm_orders(config=config, order_number=query, customer_guids=customers,
            required_order_guids=required, now=started_at)
    except Exception as exc:
        _invalidate_failed_order_lookup(actor=actor, organization=organization, deal_id=deal_id,
            pinned=pinned, required=required, config=config, source_id=source_id, started_at=started_at, operational=operational)
        raise OrderRefreshUnavailable from exc
    finished_at = clock()
    with transaction.atomic():
        user, organization, deal, current_customers = _order_lookup_scope(actor, organization, deal_id, operational=operational)
        if pinned != (deal.client_id, _object_scope(deal), current_customers):
            raise CommercialConflict('Клиент или объект сделки изменился. Повторите поиск.')
        returned = {row.order_guid for row in snapshot.rows}
        if (snapshot.full_scope or not set(required) <= set(snapshot.requested_order_guids)
                or not set(snapshot.requested_order_guids) <= returned | set(required)
                or any(row.customer_guid not in current_customers
                    or (row.number != query and row.order_guid not in required) for row in snapshot.rows)):
            raise OrderRefreshUnavailable
        apply_crm_order_snapshot(actor=user, organization=organization, snapshot=snapshot,
            config=config, now=finished_at, _lookup_deal_id=deal_id if operational else None)
        if operational:
            return
        return order_candidates(actor=user, organization=organization, deal=deal, query=query, exact=True)


def execute_order_command(*, actor, organization, operation_key, command_type, subject_id, expected_revision, changes):
    _positive_id(subject_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValidationError('Некорректная версия сделки.')
    if type(operation_key) is not str or len(operation_key) > 100:
        raise ValidationError('Некорректный ключ операции.')
    CANONICAL_KEY(operation_key)
    if command_type == 'deal.bind_order':
        if type(changes) is not dict or set(changes) != {'order_id', 'recurring'} or type(changes['recurring']) is not bool:
            raise ValidationError('Выберите заказ и тип обслуживания.')
        _positive_id(changes['order_id'])
    elif command_type == 'deal.unbind_order':
        if type(changes) is not dict or set(changes) != {'reason'} or type(changes['reason']) is not str or not 1 <= len(changes['reason'].strip()) <= 500:
            raise ValidationError('Укажите причину снятия связи (до 500 символов).')
        changes = {'reason': changes['reason'].strip()}
    else:
        raise ValidationError('Неизвестная команда связи заказа.')
    digest = _hash([actor.pk, organization.pk, operation_key, command_type, subject_id, expected_revision, changes])
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        user = _authorize('default', actor.pk, organization, 'edit_own_deals')
        user = _finance_actor(user, organization, lock=True)
        if not company_has_access(organization):
            raise PermissionDenied
        deal = _owned_deal('default', user.pk, organization.pk, subject_id)
        previous = CrmCommand.objects.select_for_update().filter(organization=organization, operation_key=operation_key).first()
        if previous:
            if previous.actor_id != user.pk or previous.command_type != command_type or previous.payload_hash != digest:
                raise CommercialConflict('Ключ операции уже использован с другими данными.')
            return dict(previous.result)
        if deal.revision != expected_revision:
            raise CommercialConflict('Карточка изменилась. Откройте её заново.')
        if deal.is_archived or deal.outcome != 'open':
            raise ValidationError('Связь можно менять только в открытой сделке.')
        link = CrmDealOrderLink.objects.select_for_update().filter(deal=deal).first()
        if command_type == 'deal.bind_order':
            order = CrmOneCOrder.objects.select_for_update().filter(pk=changes['order_id'], organization=organization).first()
            if not order or order.customer_guid not in _confirmed_customers(organization, deal.client_id, lock=True):
                raise PermissionDenied('Заказ не относится к подтверждённому клиенту сделки.')
            if not _fresh(order) or not order.order_valid or order.cancelled:
                raise CommercialConflict('Заказ изменился или требует обновления. Выберите его заново.')
            contract_problem = _service_contract_problem(deal, order)
            if contract_problem:
                raise CommercialConflict(contract_problem)
            if _recurring_required(deal) and not changes['recurring']:
                raise ValidationError('Для сделки подтверждены периоды обслуживания. Сохраните постоянное обслуживание.')
            if CrmDealOrderLink.objects.filter(order=order).exclude(deal=deal).exists():
                raise CommercialConflict('Этот заказ уже связан с другой сделкой.')
            if CrmDealDocumentLink.objects.filter(order=order).exclude(deal=deal).exists():
                raise CommercialConflict('Этот заказ уже связан с другой сделкой.')
            if link and link.order_id != order.pk:
                raise CommercialConflict('Сначала снимите прежнюю связь с указанием причины.')
            values = dict(organization=organization, deal=deal, order=order, client_id=deal.client_id,
                object_scope=_object_scope(deal), scope_fingerprint=_order_scope(order), recurring=changes['recurring'],
                confirmed_by=user, confirmed_at=timezone.now(), requires_reconciliation=False, review_reason='')
            if link:
                for key, value in values.items():
                    setattr(link, key, value)
                link.save()
            else:
                link = CrmDealOrderLink.objects.create(**values)
            reason = 'Заказ 1С связан со сделкой. ' + ('Постоянное обслуживание.' if link.recurring else 'Разовая сделка.')
        else:
            if not link:
                raise ValidationError('В сделке нет связанного заказа.')
            reason = changes['reason']
        before = deal.revision
        deal.revision += 1
        deal.save()
        command = CrmCommand.objects.create(organization=organization, operation_key=operation_key,
            command_type=command_type, payload_hash=digest, actor=user)
        CrmOrderLinkEvent.objects.create(organization=organization, deal=deal, order=link.order,
            snapshot=link.order.current_snapshot, command=command, actor=user, kind=command_type,
            recurring=link.recurring, object_scope=link.object_scope, scope_fingerprint=link.scope_fingerprint)
        if command_type == 'deal.unbind_order':
            link.delete()
        event = CrmEvent.objects.create(organization=organization, deal=deal, command=command, actor=user,
            event_type=command_type, safe_diff={'revision': {'before': before, 'after': deal.revision}}, reason=reason)
        if command_type == 'deal.bind_order':
            _reconcile_link(link, user, organization)
            deal.refresh_from_db()
        command.result = {'deal_id': deal.pk, 'event_id': event.pk, 'revision': deal.revision}
        command.save(update_fields=['result'])
        return dict(command.result)


def _reconcile_link(link, actor, organization):
    deal = CrmDeal.objects.select_for_update().select_related('pipeline', 'stage', 'client').get(pk=link.deal_id)
    order = link.order
    problem = _link_problem(link, deal)
    fully_covered = bool(order.amount is not None and order.amount > 0 and order.currency
        and order.realization_complete and order.payment_complete and not order.blockers
        and order.realized_amount is not None and order.realized_amount >= order.amount
        and order.paid_amount is not None and order.paid_amount >= order.amount)
    if deal.currency and deal.currency != order.currency:
        problem = 'Валюта сделки отличается от валюты заказа. Требуется сверка.'
    if link.completed_automatically and (problem or not fully_covered):
        if not link.requires_reconciliation:
            link.requires_reconciliation = True
            link.review_reason = problem or 'После завершения изменились реализации или оплаты. Требуется сверка.'
            link.save(update_fields=['requires_reconciliation', 'review_reason'])
            before = deal.revision
            deal.revision += 1
            deal.save()
            CrmEvent.objects.create(organization=organization, deal=deal, actor=actor,
                event_type='deal.onec_review', safe_diff={'revision': {'before': before, 'after': deal.revision}},
                reason='Данные 1С после автоматического завершения требуют сверки. Задачи и история сохранены.')
        return 'review'
    if (problem or not fully_covered or not _fresh(order) or deal.outcome != 'open' or deal.is_archived
            or link.recurring or _recurring_required(deal) or not organization.crm_v2_read or not organization.crm_v2_write):
        return 'unchanged'
    stage = CrmStage.objects.filter(pipeline=deal.pipeline, code='completed', is_active=True).first()
    if not stage or not deal.pipeline.is_active:
        return 'unchanged'
    before = deal.revision
    old_stage = deal.stage_id
    deal.stage = stage
    deal.outcome = 'won'
    deal.completed_at = _document_date(order, 'realization')
    deal.settled_at = _document_date(order, 'payment')
    deal.closed_at = timezone.now()
    deal.ordered_at = deal.ordered_at or order.order_date
    deal.close_reason = 'Заказ полностью реализован и оплачен по подтверждённым данным 1С.'
    deal.revision += 1
    deal.save()
    link.completed_automatically = True
    link.completion_snapshot = order.current_snapshot
    link.save(update_fields=['completed_automatically', 'completion_snapshot'])
    CrmEvent.objects.create(organization=organization, deal=deal, actor=actor, event_type='deal.onec_completed',
        safe_diff={'revision': {'before': before, 'after': deal.revision}, 'stage_id': {'before': old_stage, 'after': stage.pk},
                   'outcome': {'before': 'open', 'after': 'won'}},
        reason='Заказ полностью реализован и оплачен в 1С. Открытые задачи сохранены.')
    return 'completed'


def _document_date(order, kind):
    dates = []
    for document in order.documents:
        if document.get('kind') != kind:
            continue
        try:
            moment = parse_datetime(document.get('date', ''))
        except (TypeError, ValueError):
            continue
        if moment is not None and timezone.is_aware(moment) and moment <= order.source_at:
            dates.append(moment)
    return max(dates) if dates else None


def _validate_snapshot(snapshot, organization, config, now):
    from pool_service.finance_imports.odata_crm_orders import CrmOrdersSnapshot, source_identity
    from pool_service.finance_imports.odata_profit import validate_config
    from pool_service.finance_imports.odata_profit_drafts import config_from_settings
    if type(snapshot) is not CrmOrdersSnapshot:
        raise ValidationError('Требуется серверный снимок заказов 1С.')
    config = validate_config(config or config_from_settings())
    current_config = validate_config(config_from_settings())
    if (str(getattr(settings, 'ONEC_ODATA_TARGET_ORGANIZATION_ID', '')) != str(organization.pk)
            or snapshot.source_id != source_identity(config) or snapshot.source_id != source_identity(current_config)
            or set(config.organization_guids) != set(current_config.organization_guids)):
        raise PermissionDenied('Источник 1С не настроен для этой организации.')
    if (not isinstance(snapshot.source_at, datetime) or timezone.is_naive(snapshot.source_at)
            or not isinstance(snapshot.fetched_at, datetime) or timezone.is_naive(snapshot.fetched_at)
            or not now - timedelta(hours=1) <= snapshot.source_at <= snapshot.fetched_at <= now + timedelta(minutes=1)):
        raise ValidationError('Получен устаревший или некорректный снимок заказов 1С.')
    seen = set()
    discovery = isinstance(snapshot.diagnostics, dict) and snapshot.diagnostics.get('discovery_limited') is True
    if type(snapshot.full_scope) is not bool or (not snapshot.full_scope and not snapshot.requested_order_guids and not discovery):
        raise ValidationError('Не определена область снимка заказов.')
    for row in snapshot.rows:
        key = (row.organization_guid, row.order_guid)
        if row.organization_guid not in config.organization_guids or key in seen:
            raise ValidationError('Неоднозначная область заказов 1С.')
        seen.add(key)
        if not snapshot.full_scope and row.order_guid not in snapshot.requested_order_guids:
            raise ValidationError('Заказ выходит за пределы запрошенного снимка.')
        if any(type(value) is not bool for value in (row.order_valid, row.cancelled, row.realization_complete, row.payment_complete)):
            raise ValidationError('Не подтверждена полнота данных заказа.')
        for guid in (row.order_guid, row.organization_guid, *([row.customer_guid] if row.order_valid or row.customer_guid else [])):
            try:
                if str(UUID(guid)) != guid or not UUID(guid).int:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise ValidationError('Некорректная связь заказа 1С.') from None
        if not re.fullmatch('[0-9a-f]{64}', row.fingerprint):
            raise ValidationError('Отсутствует подтверждение снимка заказа.')
        if row.currency_code and not re.fullmatch('[A-Z]{3}', row.currency_code):
            raise ValidationError('Некорректная валюта заказа.')
        for value in (row.amount, row.realized_amount, row.paid_amount):
            if value is not None and (not isinstance(value, Decimal) or not value.is_finite() or abs(value) >= Decimal('1e16')
                    or value.quantize(Decimal('.01')) != value):
                raise ValidationError('Некорректная сумма заказа.')
    return config


def apply_crm_order_snapshot(*, actor, organization, snapshot, config=None, now=None, _lookup_deal_id=None):
    now = now or timezone.now()
    config = _validate_snapshot(snapshot, organization, config, now)
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        if _lookup_deal_id is None:
            actor = _finance_actor(actor, organization, lock=True)
        else:
            actor, organization, _, customers = _order_lookup_scope(actor, organization, _lookup_deal_id, operational=True)
            if snapshot.full_scope or any(row.customer_guid not in customers for row in snapshot.rows):
                raise PermissionDenied
        state = CrmOrderSyncState.objects.select_for_update().filter(organization=organization).first()
        if state and snapshot.source_at < state.source_at:
            raise CommercialConflict('Более старый снимок заказов не заменяет текущий.')
        discovery = isinstance(snapshot.diagnostics, dict) and snapshot.diagnostics.get('discovery_limited') is True
        if _lookup_deal_id is None and state and state.source_id != snapshot.source_id and not snapshot.full_scope and not discovery:
            raise CommercialConflict('Смена источника требует полного обновления заказов.')
        result = {'orders': len(snapshot.rows), 'updated': 0, 'completed': 0, 'review': 0}
        observed = set()
        for row in snapshot.rows:
            order, _ = CrmOneCOrder.objects.select_for_update().get_or_create(organization=organization,
                source_id=snapshot.source_id, external_organization=row.organization_guid, order_guid=row.order_guid,
                defaults={'customer_guid': row.customer_guid or '', 'number': row.number, 'order_date': row.order_date,
                    'source_at': snapshot.source_at, 'fetched_at': snapshot.fetched_at, 'fingerprint': row.fingerprint})
            changed = order.current_snapshot_id is None or order.fingerprint != row.fingerprint or order.missing
            if order.current_snapshot_id and (snapshot.source_at < order.source_at
                    or snapshot.source_at == order.source_at and order.fingerprint != row.fingerprint):
                raise CommercialConflict('Изменившийся заказ требует более нового снимка.')
            values = {'customer_guid': row.customer_guid or '', 'agreement_guid': row.agreement_guid or '',
                'currency_guid': row.currency_guid or '', 'currency': row.currency_code, 'number': row.number,
                'order_date': row.order_date, 'amount': row.amount, 'realized_amount': row.realized_amount,
                'paid_amount': row.paid_amount, 'realization_complete': row.realization_complete,
                'payment_complete': row.payment_complete, 'order_valid': row.order_valid, 'cancelled': row.cancelled,
                'missing': False, 'lookup_failed': False, 'blockers': list(row.blockers), 'documents': _json(row.documents),
                'fingerprint': row.fingerprint, 'source_at': snapshot.source_at, 'fetched_at': snapshot.fetched_at}
            if changed:
                evidence = CrmOrderEvidenceSnapshot.objects.create(organization=organization, order=order,
                    fingerprint=row.fingerprint, source_at=snapshot.source_at, fetched_at=snapshot.fetched_at,
                    data=_json(asdict(row)))
                values['current_snapshot'] = evidence
                result['updated'] += 1
            for key, value in values.items():
                setattr(order, key, value)
            order.save()
            from .crm_document_links import observe_realizations
            observe_realizations(order)
            observed.add(order.pk)
        scope = CrmOneCOrder.objects.filter(organization=organization)
        if not snapshot.full_scope:
            scope = scope.filter(source_id=snapshot.source_id, order_guid__in=snapshot.requested_order_guids)
        if _lookup_deal_id is not None:
            scope.filter(external_organization__in=config.organization_guids, customer_guid__in=customers,
                fetched_at__lte=snapshot.source_at).exclude(pk__in=observed).update(missing=True)
            return result
        scope.exclude(pk__in=observed).update(missing=True)
        # A validated discovery switches the source without pretending its latest
        # order window is the whole ledger. Prior links stay pinned to old evidence.
        if state and state.source_id != snapshot.source_id:
            CrmOneCOrder.objects.filter(organization=organization).exclude(source_id=snapshot.source_id).update(missing=True)
        CrmOrderSyncState.objects.update_or_create(organization=organization,
            defaults={'source_id': snapshot.source_id, 'source_at': snapshot.source_at,
                      'fetched_at': snapshot.fetched_at, 'failed': bool(state and state.failed
                          and snapshot.diagnostics.get('discovery_mode') == 'number')})
        for link in CrmDealOrderLink.objects.select_for_update().filter(organization=organization).select_related('order').order_by('pk'):
            status = _reconcile_link(link, actor, organization)
            if status in result:
                result[status] += 1
        return result


def mark_crm_order_sync_error(*, actor, organization):
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=organization.pk)
        _finance_actor(actor, organization, lock=True)
        CrmOrderSyncState.objects.filter(organization=organization).update(failed=True)
