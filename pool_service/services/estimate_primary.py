"""Preliminary primary-circuit proposals, not hydraulic/thermal sign-off.

Public model facts checked by the owner on 2026-10-20 (no prices or shop IDs):
https://aquapolis.ua/ua/nasos-aquaviva-lx-gpd25-4s-180-220-v-50-gc-65-vt-qmax-2-8-m3-ch-hmax-4-m.html
https://aquapolis.ru/media/custom_css/manuals/instructions/nasos/GPS.pdf
https://aquapolis.ru/klapan-solenoidnyj-aquaviva-2w31-latun.html
GPS temperature envelope/curve was not fully verified: deliberately unknown.
G1½ pump union thread is NOT equated to a G1 valve or an exchanger port.
"""
from decimal import Decimal
import re

CONNECTIONS = {'', 'g3/4', 'g1', 'g1.5', 'g2'}
PARAMETERS = ('primary_pump_connection', 'primary_valve_connection', 'primary_temperature',
              'primary_delta', 'primary_flow', 'primary_head', 'primary_medium')
FACTS = {
    '32528': dict(kind='circulation_pump', pattern=r'(?<![a-z0-9])gpd25-4s-180(?![a-z0-9-])', connection='g1.5',
                  flow_max='2.8', head_max='4', temperature_max='95', frequency=False),
    '35398': dict(kind='circulation_pump', pattern=r'(?<![a-z0-9])gps25-5-180(?![a-z0-9-])', connection='g1.5',
                  flow_max='2.8', head_max='5', temperature_max=None, frequency=True),
    '19817': dict(kind='solenoid_valve', pattern=r'(?<![a-z0-9])2w31(?![a-z0-9]).*\bdn20(?![a-z0-9])', connection='g3/4', temperature_max=None),
    '19818': dict(kind='solenoid_valve', pattern=r'(?<![a-z0-9])2w31(?![a-z0-9]).*\bdn25(?![a-z0-9])', connection='g1', temperature_max='120'),
    '54203': dict(kind='solenoid_valve', pattern=r'(?<![a-z0-9])2w31(?![a-z0-9]).*\bdn50(?![a-z0-9])', connection='g2', temperature_max='120'),
}


def facts(product):
    spec = FACTS.get(product.get('sku'))
    if not spec or product.get('brand', '').casefold() != 'aquaviva' or product.get('kind') != spec['kind'] or not re.search(spec['pattern'], product.get('name', ''), re.I):
        return None
    return spec


def required_flow(inputs):
    from .pool_estimates import heating_requirement
    power = heating_requirement(inputs)['power']
    calculated = (Decimal(power) / (Decimal('1.16') * Decimal(inputs['primary_delta']))
                  if power and inputs.get('primary_delta') and inputs.get('primary_medium') == 'water' else None)
    manual = Decimal(inputs['primary_flow']) if inputs.get('primary_flow') else None
    known = [x for x in (calculated, manual) if x is not None]
    return max(known) if known else None


def rejection(product, inputs):
    """Reject known mismatch, allow UNKNOWN duty only as explicitly preliminary."""
    spec = facts(product)
    if not product.get('available'):
        return 'Доступность к заказу не подтверждена.'
    if not spec:
        return 'Нет проверенной идентификации и характеристик этой модели.'
    field = 'primary_pump_connection' if spec['kind'] == 'circulation_pump' else 'primary_valve_connection'
    connection = inputs.get(field)
    if not connection:
        return 'Укажите отдельное присоединение насоса/клапана первичного контура; диаметр по цене не выбирается.'
    if connection != spec['connection']:
        return 'Присоединение не соответствует заданному; переходники не предполагаются автоматически.'
    if inputs.get('power') == '400':
        return 'Соответствие заданному питанию 400 В не подтверждено; требуется отдельная проверка питания первичного контура.'
    temperature = inputs.get('primary_temperature')
    if temperature and spec['temperature_max'] and Decimal(temperature) > Decimal(spec['temperature_max']):
        return 'Температура превышает подтверждённый предел модели.'
    if spec['kind'] == 'circulation_pump':
        flow, head = required_flow(inputs), inputs.get('primary_head')
        # Qmax and Hmax are boundary values, never a working point on a curve.
        if flow is not None and flow >= Decimal(spec['flow_max']):
            return 'Требуемый расход достиг/превысил Qmax. Нужен другой насос и проверенная рабочая кривая.'
        if head and Decimal(head) >= Decimal(spec['head_max']):
            return 'Требуемый напор достиг/превысил Hmax. Нужен другой насос и проверенная рабочая кривая.'
    return None


def select(choices, tier):
    from .estimate_catalog import select_price_tier
    if not choices:
        return None
    if choices[0]['kind'] == 'circulation_pump':
        preferred = [p for p in choices if facts(p)['frequency'] is (tier != 'economy')]
        choices = preferred or choices
    return select_price_tier(choices, tier)


def fill(snapshot, catalog, only_variant=None):
    from .estimate_catalog import apply_product
    from .pool_estimates import ACCESSORIES, exchanger_count
    filled = 0
    for tier, variant in snapshot['variants'].items():
        if only_variant and tier != only_variant:
            continue
        rows = [r for b in variant['blocks'] for r in b['rows']]
        parents = {r['id']: r for r in rows if r['kind'] == 'heater'}
        for block in variant['blocks']:
            if block['excluded']:
                continue
            for row in block['rows']:
                if row['kind'] not in ACCESSORIES or row['excluded'] or row['existing'] or row.get('selection_locked'):
                    continue
                parent = parents.get(row.get('heating_parent'))
                if not parent or exchanger_count(parent) is None or Decimal(parent['quantity']) <= 0:
                    continue
                origin = row.get('catalog')
                untouched = origin and origin['selection'] == 'auto' and row['price'] == origin['price']
                placeholder = not origin and not row['model'] and row['price'] is None and row['name'] == ACCESSORIES[row['kind']]
                if not (untouched or placeholder):
                    continue
                choices = [p for p in catalog['products'].values() if p['kind'] == row['kind'] and not rejection(p, snapshot['inputs'])]
                product = select(choices, tier)
                if product and (not origin or product['id'] != origin['id']):
                    apply_product(row, product, catalog, 'auto')
                    filled += 1
    return filled
