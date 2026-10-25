"""Preliminary catalogue proposals: unknown facts are never manufactured.

Return hard exclusions separately from missing evidence. Capacity is a lower
bound only when known; this is not a pump curve or engineering compatibility.
"""
from decimal import Decimal

CORE = {'pump', 'filter', 'heater'}


def assess(product, inputs, required_flow, profile=None):
    from .pool_estimates import heating_requirement, heating_types
    if not product.get('available'):
        from .estimate_availability import reason
        return reason(product), []
    if product.get('specification_conflict') or product.get('level_data_issue') == 'conflict':
        return 'Конфликт характеристик: ' + product.get('specification_note', 'уточните данные каталога'), []
    if product.get('embedded_role'):
        from .estimate_embedded_candidates import assess as embedded_assess
        return embedded_assess(product, profile, inputs['finish'])
    missing, kind = [], product['kind']
    if kind not in CORE:
        return 'Нет правил предварительного подбора этой категории.', []
    if kind == 'pump' and product.get('pump_facts'):
        facts = product['pump_facts']
        if facts.get('issue'):
            return facts['issue'], []
        if not facts.get('in_category'):
            return 'Принадлежность категории «Насосы для бассейнов» не подтверждена.', []
        if not facts.get('structured'):
            missing.append('характеристики насоса в карточке магазина')
        elif facts.get('prefilter') is not True:
            missing.append('отдельный префильтр перед насосом' if facts.get('prefilter') is False else 'наличие префильтра')
        if facts.get('mode_note'):
            missing.append(facts['mode_note'])
        if facts.get('purpose_note'):
            missing.append(facts['purpose_note'])
    if kind == 'pump' and product.get('filtration') is not True:
        # A vague "pump" could be an attraction/spa device: minimum identity gate.
        return 'Назначение для фильтрации с префильтром не подтверждено.', []
    if kind in {'pump', 'filter'}:
        value = product.get('nominal_flow' if kind == 'pump' else 'filter_flow')
        if value and Decimal(value) < required_flow:
            return 'Известная производительность ниже требуемого расхода.', []
        if not value: missing.append('производительность модели')
    if kind == 'pump':
        head = inputs.get('required_head')
        if head and product.get('head') and Decimal(head) != Decimal(product['head']):
            return 'Указанная рабочая точка относится к другому напору.', []
        if not head: missing.append('расчётный напор объекта')
        if not product.get('head') or not product.get('flow'): missing.append('рабочая Q–H точка насоса')
    if kind == 'heater':
        from .estimate_heater_purpose import reason as purpose_reason
        blocked = purpose_reason(product)
        if blocked:
            return blocked, []
        if product.get('heater_type') not in heating_types(inputs):
            return 'Другой тип нагрева.', []
        required = heating_requirement(inputs)['power']
        if required and product.get('heating_power') and Decimal(product['heating_power']) < Decimal(required):
            return 'Известная мощность ниже требуемой.', []
        if not required: missing.append('требуемая мощность нагрева')
        if not product.get('heating_power'): missing.append('мощность модели')
    if kind == 'pump' or kind == 'heater' and product['heater_type'] != 'exchanger':
        options = product.get('power_options', [])
        if inputs['power'] != 'unknown' and options and inputs['power'] not in options:
            return 'Известное питание модели не соответствует объекту.', []
        if inputs['power'] == 'unknown': missing.append('питание объекта')
        if not options: missing.append('питание модели')
    return None, missing


def choose(products, tier, inputs, required_flow, profile=None):
    from .estimate_catalog import select_price_tier
    eligible = [(p, assess(p, inputs, required_flow, profile)) for p in products]
    eligible = [(p, missing) for p, (blocked, missing) in eligible if not blocked]
    if not eligible: return None
    # Compare evidence before price; unknown capacity must not beat a known
    # sufficient model just because its catalogue price is lower.
    field = {'pump':'nominal_flow','filter':'filter_flow','heater':'heating_power'}.get(eligible[0][0]['kind'])
    def evidence_rank(p, missing):
        pump = p.get('pump_facts', {}) if p['kind'] == 'pump' else {}
        return (int(bool(field) and not p.get(field)),
                int(p['kind'] == 'pump' and not p.get('power_options')),
                int(bool(pump) and pump.get('prefilter') is not True),
                int(bool(pump.get('purpose_note'))), len(missing))
    best = min(evidence_rank(p, missing) for p, missing in eligible)
    choices = [p for p, missing in eligible if evidence_rank(p, missing) == best]
    known = [p for p in choices if field and p.get(field)]
    if known:
        minimum = min(Decimal(p[field]) for p in known)
        limit = minimum if choices[0]['kind'] == 'heater' else minimum * Decimal('1.25')
        choices = [p for p in known if Decimal(p[field]) <= limit]
    return select_price_tier(choices, tier)


def annotate(snapshot, variant):
    from .estimate_compatibility import physical_rows, duty
    rows = physical_rows(variant)
    flow, pump_note = duty(snapshot, variant)
    required = Decimal(snapshot['geometry']['required_flow'])
    for row in rows:
        origin = row.get('catalog')
        if not origin or row['kind'] not in CORE and not row.get('embedded_role'): continue
        product = {**origin, **row['specs'], 'name':row['name'], 'brand':row['brand'], 'kind':row['kind']}
        blocked, missing = assess(product, snapshot['inputs'], max(required, flow or required) if row['kind']=='filter' else required, variant['embedded_profile'])
        if row['kind']=='filter' and pump_note: missing.append('рабочий режим пары насос/фильтр')
        if blocked or missing or row.get('catalog_review_note'):
            reason = row.get('catalog_review_note') or blocked or 'Не подтверждены: ' + ', '.join(missing) + '.'
            row['selection_note'] = ('Проверить: ' + reason + ' Совместимость не подтверждена. '
                'Источник: сохранённые характеристики товара ' + origin['url'] + '; дата цены ' + origin['checked_at'] + '. '
                'Правило предложения: известные ограничения, полнота данных, ближайшая достаточная мощность/производительность, цена в сегменте. '
                'Неизвестные характеристики не заполнены автоматически.')[:1000]
            row['selection_status'] = 'check_core'
            variant['warnings'].append(row['name'] + ': ' + row['selection_note'])
            check = row.get('compatibility')
            row['compatibility'] = {'message': ((check['message'] + ' ') if check else '') + row['selection_note'],
                                    'can_auto': bool(check and check['can_auto']), 'kind': row.get('embedded_role') or row['kind']}
        else:
            row['selection_note'] = ''
