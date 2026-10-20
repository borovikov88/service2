"""Explicit manual rate confirmations only; validation/history have no side effects."""
from decimal import Decimal
import hashlib
import json

from django.db import transaction
from pool_service.estimate_models import PoolEstimateWorkRate
from pool_service.models import Organization
from .estimate_works import is_work, shared_base
from .pool_estimates import EstimateInputError, VARIANTS, boolean, evaluate, number, string


def identity(row):
    return hashlib.sha256(json.dumps([row['work_code'], row['unit'], row.get('finish_type', '')], ensure_ascii=False).encode()).hexdigest()


def apply_defaults(snapshot, organization, *, only_new=None):
    records = list(PoolEstimateWorkRate.objects.filter(organization=organization))
    rates = {(r.variant, r.identity): r.price for r in records}
    for key, variant in snapshot['variants'].items():
        for block in variant['blocks']:
            for row in block['rows']:
                if not is_work(row) or not row.get('work_code') or (only_new is not None and (key, row['id']) in only_new):
                    continue
                base = shared_base(row)
                candidates = [r for r in records if r.identity == identity(row)] if base else []
                price = max([Decimal(base), *(r.price for r in candidates)]) if base else rates.get((key, identity(row)))
                if price is not None and (row['price'] is None or price > Decimal(row['price'])):
                    row['price'] = str(price)
                if base and row['price'] is not None and Decimal(row['price']) == price:
                    sources = sorted({VARIANTS[r.variant][0] for r in candidates if r.price == price and r.variant in VARIANTS})
                    row['rate_source'] = ('Общая повышенная ставка организации: максимум сохранённых ставок; источник: ' + ', '.join(sources)
                                          if price > Decimal(base) else 'Общая базовая ставка, утверждена владельцем 20.10.2026')
    return evaluate(snapshot)


def confirm_price(data, organization, user):
    snapshot = evaluate(data.get('snapshot'))
    variant = string(data.get('variant'), 'Вариант', 16)
    if variant not in VARIANTS:
        raise EstimateInputError('Выберите смету.')
    bid = string(data.get('block'), 'Блок', 30)
    rid = string(data.get('row'), 'Строка', 64)
    all_quotes = boolean(data.get('all', False))
    price = number(data.get('price'), 'Цена работы', optional=True, places=2)
    block = next((b for b in snapshot['variants'][variant]['blocks'] if b['id'] == bid), None)
    if block is None:
        raise EstimateInputError('Блок не найден.')
    original = next((r for r in block['rows'] if r['id'] == rid), None)
    # A row editor may change name/quantity as well; those edits belong to one CP.
    if data.get('draft_row') is not None:
        draft = data['draft_row']
        if not isinstance(draft, dict) or draft.get('id') != rid:
            raise EstimateInputError('Некорректная строка работы.')
        if original is None:
            block['rows'].append(draft)
        else:
            block['rows'][block['rows'].index(original)] = draft
        snapshot = evaluate(snapshot)
        block = next(b for b in snapshot['variants'][variant]['blocks'] if b['id'] == bid)
    source = next((r for r in block['rows'] if r['id'] == rid), None)
    if source is None or not is_work(source):
        raise EstimateInputError('Выберите работу для изменения расценки.')
    targets = []
    for key, v in snapshot['variants'].items():
        if key != variant and not all_quotes:
            continue
        candidates = [r for b in v['blocks'] if b['id'] == bid for r in b['rows']
                      if is_work(r) and identity(r) == identity(source)]
        target = next((r for r in candidates if r['id'] == rid), None)
        if target is None and len(candidates) == 1:
            target = candidates[0]
        if target is not None:
            previous = (original['price'] if original and identity(original) == identity(source) else None) if key == variant else target['price']
            targets.append((key, target, previous))
    remembered = 0
    with transaction.atomic():
        # Serializes first insert and concurrent raises for this tenant on MySQL.
        Organization.objects.select_for_update().get(pk=organization.pk)
        shared = shared_base(source)
        existing_common = list(PoolEstimateWorkRate.objects.filter(organization=organization, identity=identity(source)).values_list('price', flat=True)) if shared else []
        minimum_raise = max([Decimal(shared), *existing_common]) if shared else None
        for key, target, previous in targets:
            target['price'] = str(price) if price is not None else None
            target['rate_source'] = 'Ручная цена текущей сметы'
            # Empty, equal and discounted rates change this quote only.
            if price is None or (previous is not None and price <= Decimal(previous)):
                continue
            if minimum_raise is not None and price <= minimum_raise:
                continue
            rate, created = PoolEstimateWorkRate.objects.get_or_create(
                organization=organization, variant=key, identity=identity(target),
                defaults={'work_code': target['work_code'], 'unit': target['unit'],
                          'finish_type': target.get('finish_type', ''), 'price': price, 'updated_by': user})
            if created:
                remembered += 1
            elif price > rate.price:
                rate.price, rate.updated_by = price, user
                rate.save(update_fields=['price', 'updated_by', 'updated_at'])
                remembered += 1
    return {'snapshot': evaluate(snapshot), 'remembered': remembered}
