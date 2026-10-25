"""Public pool-filter card facts, separate from title-based legacy hints."""
from decimal import Decimal
import re

from .estimate_catalog_params import text
from .estimate_pump_facts import number

CATEGORY = 'фильтры'
PARTS = 'запчасти для фильтров и фильтрационных установок'
FIELDS = {
    'product_type': 'тип товара',
    'type': 'тип фильтра', 'flow': 'расход воды, м³/ч',
    'diameter_mm': 'диаметр фильтра, мм', 'area_m2': 'площадь фильтрации, м²',
    'valve_included': 'шестипозиционный клапан в комплекте',
    'position': 'расположение клапана', 'connection': 'подключение',
    'model': 'модель производителя',
    'compatible_valves': 'совместимые клапаны (артикулы)',
    'compatible_filters': 'совместимые фильтры (артикулы)',
}


def skus(value):
    values = re.split(r'\s*[;,]\s*', value)
    return tuple(sorted(set(values))) if 0 < len(values) <= 50 and all(
        re.fullmatch(r'[\w][\w./-]{0,79}', v) for v in values) else None


def position(value):
    return {'боковое': 'side', 'верхнее': 'top', 'боковой': 'side', 'верхний': 'top'}.get(value)


def classify(specs, name, category, offer):
    params = {}
    for p in offer.findall('param'):
        params.setdefault(text(p.get('name')), []).append(text(p.text))
    markers = params.get('категория подбора', [])
    ancestry = [text(part) for part in category.split(' / ')]
    in_category = (set(markers) == {CATEGORY} if markers else
                   ancestry[:3] == ['бассейны', 'оборудование для бассейнов', CATEGORY])
    in_parts = (set(markers) == {PARTS} if markers else
                ancestry[:3] == ['бассейны', 'запчасти', PARTS])
    title = text(name)
    six = r'(?:шести\s*позицион|6\s*(?:-\s*ти)?\s*[- ]?\s*(?:позицион|поз\b))'
    valve_types = {'клапан', 'шестипозиционный клапан', 'клапан шестипозиционный', '6-позиционный клапан'}
    public_types = params.get('тип товара', [])
    valve = bool(re.match(r'^(?:клапан\b|' + six + r'\w*\s+клапан\b)', title)
                 and re.search(six, title)) and in_parts and (not public_types or set(public_types) <= valve_types)
    if valve and specs['kind'] == 'other':
        specs['kind'] = 'filter_valve'
    sand = params.get(FIELDS['type']) == ['песочный']
    if specs['kind'] == 'other' and in_category and sand:
        specs['kind'] = 'filter'
    if specs['kind'] not in {'filter', 'filter_valve'}:
        return
    is_filter = specs['kind'] == 'filter'
    card_labels = set(FIELDS.values()) | {'загрузка песка, кг', 'материал',
                                       'максимальное рабочее давление, бар', 'конструкция корпуса'}
    structured = any(params.get(v) for v in card_labels)
    facts = {'in_category': in_category if is_filter else in_parts,
             'structured': structured, 'issue': '', 'source': '',
             'valve_included': None, 'valve_note': '', 'position': '',
             'connection': '', 'model': '', 'flow': None, 'diameter_mm': None,
             'area_m2': None, 'velocity_m_h': None,
             'compatible_filters': [], 'compatible_valves': []}
    issues, notes = [], []
    if not facts['in_category']:
        issues.append('Принадлежность публичной категории фильтров/клапанов не подтверждена.')
    parsers = {
        'product_type': lambda v: ('filter' if is_filter else 'valve') if v in (
            {'фильтр', 'песочный фильтр', 'фильтр для бассейна'} if is_filter else valve_types) else None,
        'type': lambda v: v if v == 'песочный' else None,
        'flow': lambda v: number(v, r'(?:м[³3]/(?:ч|час)|m[³3]/h)'),
        'diameter_mm': lambda v: number(v, r'(?:мм|mm)', '10000'),
        'area_m2': lambda v: number(v, r'(?:м[²2]|m[²2])'),
        'valve_included': lambda v: {'да': True, 'нет': False}.get(v),
        'position': position, 'connection': lambda v: v or None,
        'model': lambda v: v or None,
        'compatible_valves': skus, 'compatible_filters': skus,
    }
    parsed = {}
    for key, parser in parsers.items():
        values = params.get(FIELDS[key], [])
        if not values:
            continue
        notes.append(FIELDS[key] + ': ' + '; '.join(values))
        results = [parser(v) for v in values]
        if None in results or len(set(results)) != 1:
            message = 'Неоднозначная характеристика «' + FIELDS[key] + '»: ' + '; '.join(values) + '.'
            if key == 'valve_included':
                facts['valve_note'] = message
            else:
                issues.append(message)
        else:
            parsed[key] = results[0]
    for key in facts.keys() & parsed.keys():
        facts[key] = list(parsed[key]) if key.startswith('compatible_') else parsed[key]
    if is_filter and structured:
        # Public card numbers replace the title as soon as any card facts exist.
        specs.update(filter_flow=parsed.get('flow'), nominal_flow=parsed.get('flow'), flow=None, head=None)
        if parsed.get('flow') and parsed.get('area_m2'):
            facts['velocity_m_h'] = number(format((Decimal(parsed['flow']) / Decimal(parsed['area_m2'])).normalize(), 'f'))
    facts['issue'] = ' '.join(dict.fromkeys(issues))[:1000]
    facts['source'] = ('Характеристики карточки магазина: ' + '; '.join(notes) if structured else
                       'Старый каталог: характеристики фильтра из текста; требуется заполнение карточки магазина.')[:2000]
    specs['filter_facts' if is_filter else 'filter_valve'] = facts


def normalize(raw):
    from .pool_estimates import boolean, string, EstimateInputError
    if not isinstance(raw, dict) or not raw:
        return {}
    out = {'in_category': boolean(raw.get('in_category', False)),
           'structured': boolean(raw.get('structured', False)),
           'valve_included': None if raw.get('valve_included') is None else boolean(raw['valve_included'])}
    for key, limit in [('issue', 1000), ('source', 2000), ('valve_note', 500),
                       ('position', 10), ('connection', 300), ('model', 120)]:
        out[key] = string(raw.get(key, ''), 'Характеристика фильтра', limit)
    if out['position'] not in {'', 'side', 'top'}:
        raise EstimateInputError('Некорректное расположение клапана фильтра.')
    for key in ('flow', 'diameter_mm', 'area_m2', 'velocity_m_h'):
        value = raw.get(key)
        if value is not None and number(str(value)) is None:
            raise EstimateInputError('Некорректные числовые характеристики фильтра.')
        out[key] = number(str(value)) if value is not None else None
    for key in ('compatible_filters', 'compatible_valves'):
        values = raw.get(key, [])
        if not isinstance(values, list) or len(values) > 50 or any(not isinstance(v, str) for v in values):
            raise EstimateInputError('Некорректная совместимость клапана фильтра.')
        parsed = skus(';'.join(values)) if values else ()
        if parsed is None:
            raise EstimateInputError('Некорректные артикулы совместимости клапана фильтра.')
        out[key] = list(parsed)
    return out


def assess(product):
    facts = product.get('filter_facts') or {}
    if not facts:
        return None, []  # Saved legacy estimates remain readable.
    if facts.get('issue'):
        return facts['issue'], []
    if not facts.get('in_category'):
        return 'Принадлежность категории «Фильтры» не подтверждена.', []
    missing = []
    if not facts.get('structured'):
        missing.append('характеристики фильтра в карточке магазина')
    if facts.get('valve_note'):
        missing.append(facts['valve_note'])
    elif facts.get('valve_included') is None:
        missing.append('шестипозиционный клапан в комплекте фильтра')
    return None, missing
