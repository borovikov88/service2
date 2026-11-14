"""Trips from quoted purchase quantities and explicit vehicle capacities."""
from decimal import Decimal, ROUND_CEILING

MATERIALS = ('sand', 'concrete', 'rebar', 'gravel')
TITLES = {'sand': 'Доставка песка', 'concrete': 'Доставка бетона',
          'rebar': 'Доставка арматуры', 'gravel': 'Доставка щебня'}
BASES = {'delivery_' + material for material in MATERIALS}
SOURCES = {'sand': ({'material_sand'}, {'pkg_sand_tonnes'}),
           'concrete': ({'material_concrete_bowl', 'material_concrete_base'}, {'pkg_concrete_total', 'pkg_base_volume'}),
           'rebar': ({'material_rebar'}, {'pkg_rebar_mass', 'pkg_rebar_tonnes'})}
FLEET_VOLUMES = {'sand': ('5', '10', '20'), 'concrete': ('5', '7', '9', '10'),
                'gravel': ('5', '10', '20')}


def lower_middle(values):
    ordered = sorted(Decimal(value) for value in values)
    return ordered[(len(ordered) - 1) // 2]


DEFAULTS = {'delivery_' + material + '_capacity': str(lower_middle(values))
            for material, values in FLEET_VOLUMES.items()}
DEFAULTS.update(delivery_rebar_capacity='20', delivery_sand_payload='12',
                delivery_gravel_payload='12', delivery_gravel_density='1.5')


def defaults(raw):
    """Proposals belong to an explicit new calculation/recalculation, not load."""
    return {**DEFAULTS, **raw} if isinstance(raw, dict) else raw


def normalize(raw):
    from .pool_estimates import number
    fields = [('delivery_' + material + '_capacity', '.001') for material in MATERIALS]
    fields.extend([('delivery_gravel_volume', '0'), ('delivery_sand_payload', '.001'),
                   ('delivery_gravel_payload', '.001'), ('delivery_gravel_density', '.1')])
    return {field: (str(value) if value is not None else None)
            for field, minimum in fields if field in raw
            for value in [number(raw[field], field, optional=True, minimum=minimum,
                                 maximum='5' if field == 'delivery_gravel_density' else '1000000')]}


def initialize(snapshot):
    if snapshot.get('construction_version') != 1:
        return
    from .pool_estimates import make_row, EstimateInputError
    snapshot['delivery_version'] = 1
    for variant in snapshot['variants'].values():
        if variant.get('delivery_initialized'):
            continue
        allrows = [r for block in variant['blocks'] for r in block['rows']]
        construction = next(b for b in variant['blocks'] if b['id'] == 'construction')
        for material in MATERIALS:
            code = 'delivery_' + material
            matches = [r for r in allrows if r['id'] == code or r.get('work_code') == code]
            if not matches and material == 'gravel' and not any(r['name'] == TITLES[material] for r in allrows):
                if len(construction['rows']) >= 100:
                    raise EstimateInputError('Недостаточно места для доставки щебня.')
                row = make_row(code, TITLES[material], 'рейс', None, code, 'work')
                row['work_code'] = code
                construction['rows'].append(row)
                matches = [row]
            for row in matches:
                # Adopt only the old empty default; user-edited quantities,
                # rates, units, exclusions and deleted rows are left intact.
                if (row['kind'] == 'work' and row['name'] == TITLES[material]
                        and row['unit'] == 'рейс' and not row['basis'] and row['quantity'] is None
                        and row['price'] is None and not row.get('catalog') and not row.get('model')
                        and not row.get('excluded') and not row.get('existing')
                        and not row.get('selection_locked') and not row.get('unit_price_manual')):
                    row.update(basis=code, auto_quantity=True)
        variant['delivery_initialized'] = True


def material_rows(variant, material):
    ids, bases = SOURCES[material]
    return [r for block in variant['blocks'] if block['id'] == 'construction_materials' and not block['excluded']
            for r in block['rows'] if not r['excluded'] and not r['existing']
            and (r['id'] in ids or r.get('basis') in bases)]


def volumes(snapshot, variant, material):
    """Return separate concrete stages, but one aggregate for sand or rebar."""
    inputs = snapshot['inputs']
    if material == 'gravel':
        materials = next(b for b in variant['blocks'] if b['id'] == 'construction_materials')
        if materials['excluded']:
            return [Decimal(0)], ''
        value = inputs.get('delivery_gravel_volume')
        return ([Decimal(value)], '') if value is not None else (None, 'Укажите объём щебня для доставки.')
    values = []
    for row in material_rows(variant, material):
        if row['quantity'] is None or row.get('construction_unit_conflict'):
            return None, 'Количество или единица закупки материала не подтверждены.'
        value = Decimal(row['quantity'])
        if not value:
            values.append(Decimal(0))
            continue
        if material == 'concrete':
            if row['unit'] != 'м³':
                return None, 'Для доставки бетона нужен подтверждённый объём закупки в м³.'
        elif material == 'rebar':
            if row['unit'] == 'кг':value /= 1000
            elif row['unit'] != 'т':return None, 'Для доставки арматуры нужна масса закупки в т или кг.'
        elif row['unit'] in {'т', 'кг'}:
            density = inputs.get('construction_sand_density')
            if density is None:
                return None, 'Для перевода массы песка в м³ укажите плотность песка, т/м³.'
            value = value / (1000 if row['unit'] == 'кг' else 1) / Decimal(density)
        elif row['unit'] != 'м³':
            return None, 'Для доставки песка нужны м³ либо масса в т/кг с заданной плотностью.'
        values.append(value)
    return (values if material == 'concrete' else [sum(values, Decimal(0))]), ''


def trips(snapshot, variant, material):
    values, note = volumes(snapshot, variant, material)
    if values is None:
        return None, note
    if not any(values):
        return '0', 'Нет активного количества материала для доставки: 0 рейсов.'
    capacity = snapshot['inputs'].get('delivery_' + material + '_capacity')
    unit = 'т' if material == 'rebar' else 'м³'
    if capacity is None:
        return None, 'Укажите вместимость машины, ' + unit + '/рейс; количество рейсов пока не определено.'
    capacity = Decimal(capacity)
    result = sum((v / capacity).to_integral_value(rounding=ROUND_CEILING) for v in values)
    payload_note = ''
    payload = snapshot['inputs'].get('delivery_' + material + '_payload') if material in {'sand', 'gravel'} else None
    if payload is not None:
        density = snapshot['inputs'].get('construction_sand_density' if material == 'sand' else 'delivery_gravel_density')
        if density is None:
            return None, 'Задана грузоподъёмность машины; укажите плотность материала, т/м³, для проверки ограничения по массе.'
        density, payload = Decimal(density), Decimal(payload)
        if material == 'gravel':
            mass = sum(values) * density
        else:
            # Use purchased mass directly when present, avoiding a second
            # rounded mass/density conversion at exact payload boundaries.
            mass = sum((Decimal(r['quantity']) * density if r['unit'] == 'м³' else
                        Decimal(r['quantity']) / (1000 if r['unit'] == 'кг' else 1))
                       for r in material_rows(variant, material))
        result = max(result, (mass / payload).to_integral_value(rounding=ROUND_CEILING))
        effective = min(capacity, payload / density)
        payload_note = ' Грузоподъёмность ' + format(payload, 'f') + ' т; плотность ' + format(density, 'f') + ' т/м³; фактическая вместимость не более ' + format(effective, 'f') + ' м³/рейс.'
    if result > 1000000:
        return None, 'Расчёт превышает допустимый предел 1 000 000 рейсов; уточните количество материала и вместимость машины.'
    stages = ' Бетон по отдельным маркам/этапам округлён отдельно, затем рейсы сложены.' if material == 'concrete' else ''
    return str(result), 'По сметным количествам закупки: ' + format(sum(values), 'f') + ' ' + unit + '; вместимость ' + format(capacity, 'f') + ' ' + unit + '/рейс; округление вверх до целого рейса.' + payload_note + stages


def sync(snapshot):
    if snapshot.get('delivery_version') != 1 or snapshot.get('construction_version') != 1:
        return
    for variant in snapshot['variants'].values():
        for block in variant['blocks']:
            for row in block['rows']:
                if row.get('basis') not in BASES or row['kind'] != 'work' or row['existing']:
                    continue
                if not row['auto_quantity']:
                    if row.get('delivery_computed_note') and row.get('selection_note') == row['delivery_computed_note']:
                        row['selection_note'] = 'Количество рейсов задано вручную.'
                    row['delivery_computed_note'] = ''
                    continue
                if row['unit'] != 'рейс':
                    value, note = None, 'Автоматическое количество доставки требует единицу «рейс»; единица строки сохранена.'
                else:
                    value, note = trips(snapshot, variant, row['basis'].removeprefix('delivery_'))
                row['quantity'] = value
                row['selection_note'] = row['delivery_computed_note'] = note[:1000]
