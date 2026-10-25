"""One separately priced six-position valve only when the filter excludes it."""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256
import re

TITLE = 'Шестипозиционный клапан: подобрать модель'
OFF = 'Связанный фильтр исключён или отсутствует'
PRICE_REVIEW = 'Комплектность фильтра в текущей карточке отличается от сохранённой.'


def rows(variant):
    return [r for b in variant['blocks'] for r in b['rows']]


def protected(row):
    origin = row.get('catalog')
    return (row.get('existing') or row.get('selection_locked') or row.get('unit_price_manual')
            or not row.get('auto_quantity') or row.get('basis') != 'filter_valve'
            or row.get('excluded') and row.get('reason') != OFF
            or row.get('unit') != 'шт.'
            or origin and (origin.get('selection') != 'auto' or row['price'] != origin['price'])
            or not origin and (row.get('price') is not None or row.get('model') or row.get('name') != TITLE))


def included(parent):
    facts = ((parent or {}).get('catalog') or {}).get('filter_facts') or {}
    # New card evidence separates completeness from hydraulic conflicts. Keep
    # the old interpretation for frozen snapshots until explicit card refresh.
    if facts.get('valve_note') or facts.get('issue') and not facts.get('valve_independent'):
        return None
    return facts.get('valve_included')


def token(parent):
    origin = parent.get('catalog') or {}
    return origin.get('id', '') + ':' + {True: 'yes', False: 'no', None: 'unknown'}[included(parent)]


def quantity(parent):
    if not parent or parent.get('quantity') is None:
        return None
    count = Decimal(parent['quantity'])
    if parent.get('unit') not in {'шт.', 'шт', 'компл.', 'компл', 'комплект'} or count != count.to_integral_value():
        return None
    return str(count)


def initialize(snapshot, only=None):
    snapshot['filter_valve_version'] = 1
    for tier, variant in snapshot['variants'].items():
        if not only or tier == only:
            variant['filter_valve_active'] = True
    sync(snapshot, only)


def sync(snapshot, only=None):
    if snapshot.get('filter_valve_version') != 1:
        return
    from .pool_estimates import make_row
    for tier, variant in snapshot['variants'].items():
        if only and tier != only or not variant.get('filter_valve_active'):
            continue
        allrows = rows(variant)
        parents = {r['id']: r for r in allrows if r['kind'] == 'filter'}
        hidden = {r['id'] for b in variant['blocks'] if b['excluded'] for r in b['rows']}
        remembered = variant.setdefault('filter_valve_parents', {})
        target = next(b for b in variant['blocks'] if b['id'] == 'equipment')
        remove = set()
        # Missing parents and included valves remove only untouched generated rows.
        # Manual choices, quantities, exclusions and user deletions remain protected.
        for row in allrows:
            if row['kind'] != 'filter_valve' or not row.get('filter_parent'):
                continue
            parent = parents.get(row['filter_parent'])
            if not parent or parent['existing'] or included(parent) is not False:
                if not protected(row):
                    remove.add(row['id'])
                if not parent or parent['existing']:
                    remembered.pop(row['filter_parent'], None)
        for pid, parent in parents.items():
            linked = [r for r in allrows if r['kind'] == 'filter_valve'
                      and r.get('filter_parent') == pid and r['id'] not in remove]
            manual = [r for r in linked if protected(r)]
            automatic = [r for r in linked if not protected(r)]
            # A manual valve already bound to this filter suppresses an extra quote.
            keep = None if manual else next(iter(automatic), None)
            remove.update(r['id'] for r in automatic if r is not keep)
            state, current_token = included(parent), token(parent)
            inactive = parent['existing'] or parent['excluded'] or pid in hidden or quantity(parent) == '0.000' or quantity(parent) == '0'
            unbound = any(r['kind'] == 'filter_valve' and not r.get('filter_parent')
                          and r['id'] not in remove for r in allrows)
            changed = remembered.get(pid) != current_token
            if state is False and not inactive and not linked and not unbound and changed and len(target['rows']) < 100:
                rid = 'filter_valve_' + sha256(pid.encode()).hexdigest()[:20]
                if not any(r['id'] == rid for r in allrows):
                    keep = make_row(rid, TITLE, 'шт.', quantity(parent), 'filter_valve', 'filter_valve')
                    keep['filter_parent'] = pid
                    target['rows'].append(keep)
                    allrows.append(keep)
            # Do not consume the initial decision while the parent is excluded.
            if not inactive:
                remembered[pid] = current_token
            if keep:
                if inactive and not keep['excluded']:
                    keep.update(excluded=True, reason=OFF)
                elif not inactive and keep['excluded'] and keep.get('reason') == OFF:
                    keep.update(excluded=False, reason='')
                keep['quantity'] = quantity(parent)
        if remove:
            for block in variant['blocks']:
                block['rows'] = [r for r in block['rows'] if r['id'] not in remove]


def assess(product, parent=None):
    from .estimate_availability import reason as stock_reason
    if not product.get('available'):
        return stock_reason(product)
    facts = product.get('filter_valve') or {}
    if product.get('kind') != 'filter_valve' or not facts.get('in_category'):
        return 'Назначение как отдельного шестипозиционного клапана не подтверждено.'
    if product.get('specification_conflict') or facts.get('issue'):
        return facts.get('issue') or 'Противоречивые характеристики клапана.'
    if not parent or not parent.get('catalog'):
        return 'Не выбран связанный фильтр для проверки клапана.'
    if parent.get('existing'):
        return 'Для существующего фильтра необходимость новой покупки клапана требует ручного подтверждения.'
    origin = parent['catalog']
    filter_facts = origin.get('filter_facts') or {}
    if filter_facts.get('issue'):
        return filter_facts['issue']
    if included(parent) is not False:
        return 'Не подтверждено отсутствие шестипозиционного клапана в комплекте фильтра.'
    filter_sku, valve_sku = origin.get('sku', '').casefold(), product.get('sku', '').casefold()
    allowed_filters = facts.get('compatible_filters', [])
    allowed_valves = filter_facts.get('compatible_valves', [])
    if allowed_filters and filter_sku not in allowed_filters or allowed_valves and valve_sku not in allowed_valves:
        return 'Явный список совместимости не подтверждает эту пару фильтр/клапан.'
    if not ((allowed_filters and filter_sku in allowed_filters) or (allowed_valves and valve_sku in allowed_valves)):
        return 'Совместимость клапана с артикулом выбранного фильтра не подтверждена карточкой магазина; одного бренда или диаметра недостаточно.'
    if facts.get('position') and filter_facts.get('position') and facts['position'] != filter_facts['position']:
        return 'Расположение клапана не соответствует выбранному фильтру.'
    first, second = connections(filter_facts.get('connection', '')), connections(facts.get('connection', ''))
    for unit in first.keys() & second.keys():
        if first[unit].isdisjoint(second[unit]):
            return 'Явные размеры подключения фильтра и клапана различаются; переходная обвязка не подтверждена.'
    if any(raw and not parsed for raw, parsed in ((filter_facts.get('connection'), first), (facts.get('connection'), second))):
        return 'Формат подключения фильтра или клапана требует ручной проверки.'
    return None


def connections(value):
    """Only explicit simple sizes, without converting nominal pipe standards."""
    value = value.casefold().strip()
    simple = r'\d+(?:[.,]\d+)?\s*(?:мм|mm|дюйм\w*|inch(?:es)?|["″])'
    if not re.fullmatch(simple + r'(?:\s*[/;]\s*' + simple + r')*', value):
        return {}
    numbers = r'(\d+(?:[.,]\d+)?)'
    out = {}
    for unit, suffix in [('mm', r'(?:мм|mm)\b'), ('inch', r'(?:дюйм\w*|inch(?:es)?\b|["″])')]:
        values = {Decimal(v.replace(',', '.')) for v in re.findall(numbers + r'\s*' + suffix, value)}
        if values:
            out[unit] = values
    return out


def review_price(row, product):
    if row['kind'] != 'filter' or not row.get('catalog'):
        return
    current = {'catalog': {'filter_facts': product.get('filter_facts', {})}}
    if included(row) != included(current):
        row['catalog_review_note'] = PRICE_REVIEW + ' Проверьте отдельный клапан; принять новые характеристики можно явным автоподбором или выбором фильтра.'
    elif row.get('catalog_review_note', '').startswith(PRICE_REVIEW):
        row['catalog_review_note'] = ''


def clear(row, note):
    row.update(name=TITLE, model='', brand='', catalog=None, price=None,
               catalog_review_note='', selection_note='Проверить: ' + note)
    row['specs'] = dict.fromkeys(('flow', 'head', 'filter_flow', 'backwash_flow'))


def fill(snapshot, catalog, only=None):
    from .estimate_catalog import apply_product, refresh_auto_facts, select_price_tier
    # An explicit catalogue refresh adopts current completeness evidence for the
    # selected filter, including a manually priced filter. It never changes its
    # saved model, hydraulic row specifications, quantity or frozen price date.
    for tier, variant in snapshot['variants'].items():
        if only and tier != only:
            continue
        for parent in rows(variant):
            origin = parent.get('catalog')
            current = catalog['products'].get(origin['id']) if origin and parent['kind'] == 'filter' else None
            if current and current['kind'] == 'filter':
                origin['filter_facts'] = deepcopy(current.get('filter_facts', {}))
    initialize(snapshot, only)
    count = 0
    for tier, variant in snapshot['variants'].items():
        if only and tier != only:
            continue
        parents = {r['id']: r for r in rows(variant) if r['kind'] == 'filter'}
        for block in variant['blocks']:
            if block['excluded']:
                continue
            for row in block['rows']:
                if row['kind'] != 'filter_valve' or row['excluded'] or not row.get('filter_parent'):
                    continue
                parent = parents.get(row['filter_parent'])
                origin = row.get('catalog')
                current = catalog['products'].get(origin['id']) if origin else None
                if current:
                    issue = assess(current, parent)
                    row['catalog_review_note'] = ('Текущая выгрузка: ' + issue)[:1000] if issue else ''
                if protected(row) or not parent or included(parent) is not False:
                    continue
                choices = [p for p in catalog['products'].values() if p['kind'] == 'filter_valve' and not assess(p, parent)]
                chosen = select_price_tier(choices, tier)
                if chosen:
                    if origin and origin['id'] == chosen['id']:
                        refresh_auto_facts(row, chosen, catalog)
                    else:
                        apply_product(row, chosen, catalog, 'auto')
                        count += 1
                elif origin:
                    clear(row, assess(current or {**origin, 'kind': 'filter_valve'}, parent) or 'Подходящая замена клапана не найдена.')
    sync(snapshot, only)
    return count


def annotate(snapshot, variant):
    if snapshot.get('filter_valve_version') != 1 or not variant.get('filter_valve_active'):
        return
    allrows = rows(variant)
    parents = {r['id']: r for r in allrows if r['kind'] == 'filter'}
    hidden = {r['id'] for b in variant['blocks'] if b['excluded'] for r in b['rows']}
    for pid, parent in parents.items():
        if parent['excluded'] or pid in hidden or parent['existing']:
            continue
        facts = (parent.get('catalog') or {}).get('filter_facts') or {}
        if parent.get('catalog') and included(parent) is None:
            variant['warnings'].append(parent['name'] + ': проверить, входит ли шестипозиционный клапан в комплект. ' + facts.get('valve_note', ''))
        if included(parent) is False and not any(r['kind'] == 'filter_valve' and r.get('filter_parent') == pid
                                              and not r['excluded'] for r in allrows):
            variant['warnings'].append(parent['name'] + ': клапан не входит в комплект. Отдельная строка отсутствует или исключена; проверьте состав и ручные позиции.')
    for row in allrows:
        if row['kind'] != 'filter_valve' or row['excluded'] or row['id'] in hidden:
            continue
        parent = parents.get(row.get('filter_parent'))
        origin = row.get('catalog')
        note = row.get('catalog_review_note')
        if not parent:
            note = 'Не определён связанный фильтр; подтвердите необходимость и совместимость клапана.'
        elif parent['existing']:
            note = 'Фильтр отмечен как существующий; ручная позиция клапана сохранена, необходимость новой покупки требует подтверждения.'
        elif included(parent) is True:
            note = 'Клапан входит в комплект выбранного фильтра; ручная отдельная позиция сохранена, проверьте дублирование.'
        elif included(parent) is None:
            note = 'Комплектность выбранного фильтра не подтверждена; ручная позиция клапана сохранена.'
        elif not origin:
            note = 'Шестипозиционный клапан не входит в комплект; модель, цена и совместимость с выбранным фильтром требуют проверки.'
        else:
            note = note or assess({**origin, 'kind': 'filter_valve'}, parent)
        if parent and quantity(parent) is None:
            note = (note + ' ' if note else '') + 'Для количества клапанов нужно целое количество фильтров в штуках/комплектах.'
        if note:
            row['selection_note'] = ('Проверить: ' + note)[:1000]
            row['compatibility'] = {'message': row['selection_note'], 'can_auto': False, 'kind': 'filter_valve'}
            variant['warnings'].append(row['selection_note'])
        else:
            row['selection_note'] = ''
