"""Row-level preliminary checks. Never infer parallel pump plumbing or pump curves."""
from decimal import Decimal

from . import estimate_embedded as embedded
from .estimate_level_shared import filtration_quantity


def physical_rows(variant):
    return [r for b in variant['blocks'] for r in b['rows']
            if (not b['excluded'] and not r['excluded']) or r['existing']]


def duty(snapshot, variant):
    pumps = [r for r in physical_rows(variant) if r['kind'] == 'pump']
    if len(pumps) != 1 or Decimal(pumps[0]['quantity']) != 1:
        return None, 'Нужна проверка гидравлической схемы: один рабочий насос не определён.'
    pump = pumps[0]
    if pump['unit'] not in ('шт.', 'компл.'):
        return None, 'Проверьте единицу измерения насоса: для расчёта нужен один насос в штуках или один комплект.'
    flow = pump['specs'].get('flow')
    nominal = (pump.get('catalog') or {}).get('nominal_flow')
    head = snapshot['inputs']['required_head']
    confirmed = flow and head and pump['specs'].get('head') and Decimal(head) == Decimal(pump['specs']['head'])
    if confirmed:
        value = Decimal(flow)
    elif flow or nominal:
        value = max(Decimal(flow or 0), Decimal(nominal or 0))
    else:
        return None, 'Укажите производительность насоса, чтобы проверить совместимость.'
    return max(value, Decimal(snapshot['geometry']['required_flow'])), '' if confirmed else 'Предварительно по каталогу.'


def annotate(snapshot, variant):
    """Computed metadata only; incoming warnings and repair permissions are discarded."""
    active = physical_rows(variant)
    flow, note = duty(snapshot, variant)
    filters = [r for r in active if r['kind'] == 'filter']
    for b in variant['blocks']:
        for row in b['rows']:
            row['compatibility'] = None
            role = row['kind']
            if row not in active or role not in ('filter', 'skimmer', 'inlet'):
                continue
            editable = not b['excluded'] and not row['excluded'] and not row['existing']
            message, can_auto = '', False
            if flow is None:
                message = note
            elif role == 'filter':
                if len(filters) != 1 or Decimal(row['quantity']) != 1:
                    message = 'Несколько фильтров: проверьте гидравлическую схему и распределение расхода.'
                else:
                    capacity = row['specs'].get('filter_flow')
                    if not capacity or Decimal(capacity) < flow:
                        capacity_text = format(Decimal(capacity).normalize(), 'f') + ' м³/ч' if capacity else 'расход не указан'
                        message = f'Проверьте фильтр: {capacity_text}, требуется от {flow.normalize():f} м³/ч. ' + note
                        can_auto = editable and row['unit'] in ('шт.', 'компл.')
            else:
                rows = [r for r in active if r['kind'] == role]
                if any(r['unit'] != 'шт.' for r in rows):
                    row['compatibility'] = {'message': 'Проверьте единицу измерения закладных: для расчёта количества нужны штуки.', 'can_auto': False, 'kind': role}
                    continue
                actual = sum(filtration_quantity(r) for r in rows)
                required = max(1 if role == 'skimmer' else 2,
                               int(embedded.ceil_quantity(flow / Decimal(snapshot['inputs']['skimmer_flow' if role == 'skimmer' else 'inlet_flow']))))
                if role == 'skimmer':
                    required = max(required, int(embedded.ceil_quantity(Decimal(snapshot['geometry']['surface']) / Decimal(snapshot['inputs']['skimmer_area']))))
                if actual < required:
                    noun = 'скиммеров' if role == 'skimmer' else 'форсунок'
                    message = f'Проверьте количество {noun}: {actual.normalize():f}, нужно от {required} для {flow.normalize():f} м³/ч. ' + note
                    can_auto = editable and row['unit'] == 'шт.' and row.get('embedded_role') == role
            if message:
                row['compatibility'] = {'message': message.strip(), 'can_auto': bool(can_auto), 'kind': role}


def repair(raw, variant_key, block_id, row_id, catalog=None):
    from .pool_estimates import evaluate, EstimateInputError, VARIANTS
    from .estimate_catalog import apply_product, choose_filtration_product
    result = evaluate(raw)
    if not isinstance(variant_key, str) or variant_key not in VARIANTS:
        raise EstimateInputError('Выберите смету.')
    variant = result['variants'][variant_key]
    row = next((r for b in variant['blocks'] if b['id'] == block_id for r in b['rows'] if r['id'] == row_id), None)
    if not row or not row.get('compatibility') or not row['compatibility']['can_auto']:
        raise EstimateInputError('Для этой позиции автоматическое исправление недоступно. Проверьте состав и характеристики.')
    role = row['compatibility']['kind']
    flow, _ = duty(result, variant)
    if role == 'filter':
        from .estimate_candidates import assess
        choices = [p for p in (catalog or {}).get('products', {}).values() if p['kind'] == 'filter'
                   and p.get('filter_flow') and not assess(p, result['inputs'], flow)[0]]
        if not choices:
            raise EstimateInputError('Подходящий фильтр в выбранном сегменте не найден. Откройте каталог и проверьте другие бренды. Текущая позиция сохранена.')
        apply_product(row, choose_filtration_product(choices, variant_key), catalog)
        row['selection_locked'] = True  # explicit single-row replacement, keep it on broad recalculation
        from .estimate_filter_valve import initialize
        initialize(result, variant_key)
    else:
        required = max(1 if role == 'skimmer' else 2, int(embedded.ceil_quantity(flow / Decimal(result['inputs']['skimmer_flow' if role == 'skimmer' else 'inlet_flow']))))
        if role == 'skimmer':
            required = max(required, int(embedded.ceil_quantity(Decimal(result['geometry']['surface']) / Decimal(result['inputs']['skimmer_area']))))
        others = sum(filtration_quantity(r) for r in physical_rows(variant)
                     if r['id'] != row_id and r['kind'] == role)
        row['quantity'] = embedded.ceil_quantity(Decimal(required) - others)
        if role == 'inlet' and 'level_quantity' in row:
            row['filtration_quantity'] = row['quantity']
            row['quantity'] = str(Decimal(row['quantity']) + Decimal(row['level_quantity']))
        row['auto_quantity'] = False  # other existing/manual rows already contribute to the total
        row['selection_locked'] = True
    return evaluate(result)
