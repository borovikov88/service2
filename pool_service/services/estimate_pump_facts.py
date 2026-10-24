"""Public pool-pump card facts. Qmax/Hmax never form an operating point."""
from decimal import Decimal, InvalidOperation
import re

from .estimate_catalog_params import text

CATEGORY = 'насосы для бассейнов'
FIELDS = {
    'type': 'тип товара', 'purpose': 'назначение', 'prefilter': 'префильтр',
    'voltage': 'напряжение', 'flow': 'расход воды, м³/ч',
    'head': 'напор при указанной производительности, м',
    'mode': 'режим указанной производительности', 'phases': 'число фаз',
    'frequency': 'частота питания, гц',
}


def number(value, unit='', maximum='100000'):
    match = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(?:' + unit + r')?', value)
    if not match:
        return None
    try:
        result = Decimal(match[1].replace(',', '.'))
        return format(result.normalize(), 'f') if 0 < result <= Decimal(maximum) else None
    except InvalidOperation:
        return None


def voltage(value):
    match = re.fullmatch(r'(220|230|380|400)\s*[вv]?', value)
    return ('230' if match[1] in {'220', '230'} else '400') if match else None


def purpose(value):
    values = set(re.split(r'\s*[;,/]\s*', value))
    known = {'фильтрация', 'циркуляция', 'гидромассаж', 'противоток', 'аттракционы',
             'фонтан', 'фонтаны', 'дренаж', 'дозирование', 'спа', 'spa'}
    return tuple(sorted(values)) if values and values <= known else None


def classify(specs, name, description, category, url, offer):
    params = {}
    for param in offer.findall('param'):
        params.setdefault(text(param.get('name')), []).append(text(param.text))
    markers = params.get('категория подбора', [])
    ancestry = [text(part) for part in category.split(' / ')]
    in_category = (set(markers) == {CATEGORY} if markers else
                   ancestry[:3] == ['бассейны', 'оборудование для бассейнов', CATEGORY])
    if specs['kind'] != 'pump' and not (in_category and params.get('тип товара') == ['насос']):
        return
    # A known heater/primary-circuit/accessory identity cannot be relabelled by
    # a category marker or a generic public type alone.
    if specs['kind'] not in {'pump', 'other'}:
        return
    issues = []
    structured = any(params.get(label) for label in FIELDS.values())
    facts = {'in_category': in_category, 'structured': structured,
             'prefilter': None, 'issue': '', 'source': ''}
    notes = []
    if not in_category:
        issues.append('Принадлежность категории «Насосы для бассейнов» не подтверждена.')
    if structured:
        parsers = {
            'type': lambda v: v if v == 'насос' else None,
            'purpose': purpose,
            'prefilter': lambda v: {'да': True, 'нет': False}.get(v),
            'voltage': voltage,
            'flow': lambda v: number(v, r'(?:м[³3]/(?:ч|час)|m[³3]/h)'),
            'head': lambda v: number(v, r'[мm]', '1000'),
            'phases': lambda v: v if v in {'1', '3'} else None,
            'frequency': lambda v: number(v, r'(?:гц|hz)', '1000'),
        }
        parsed = {}
        for key, parser in parsers.items():
            values = params.get(FIELDS[key], [])
            if not values:
                continue
            notes.append(FIELDS[key] + ': ' + '; '.join(values))
            results = [parser(v) for v in values]
            if None in results or len(set(results)) != 1:
                issues.append('Неоднозначная характеристика «' + FIELDS[key] + '»: ' + '; '.join(values) + '.')
            else:
                parsed[key] = results[0]
        duties = parsed.get('purpose', ())
        if duties and ('фильтрация' not in duties or set(duties) - {'фильтрация', 'циркуляция'}):
            issues.append('Назначение насоса не подтверждает обычную фильтрацию бассейна: ' + '; '.join(duties) + '.')
        facts['prefilter'] = parsed.get('prefilter')
        # The public operating-mode field must describe the same single Q-H
        # point. A table of curves or maximum values is not a substitute.
        modes = params.get(FIELDS['mode'], [])
        for value in modes:
            notes.append(FIELDS['mode'] + ': ' + value)
            match = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*м[³3]/ч\s+при\s+(\d+(?:[.,]\d+)?)\s*м\s*;\s*(220|230|380|400)\s*в', value)
            if not match or any(parsed.get(k) is None or parsed[k] != p(v) for k, p, v in (
                    ('flow', number, match[1] if match else ''),
                    ('head', number, match[2] if match else ''),
                    ('voltage', voltage, match[3] if match else ''))):
                issues.append('Режим указанной производительности не подтверждает единую рабочую точку и питание.')
        specs.update(kind='pump', filtration='фильтрация' in duties,
                     nominal_flow=parsed.get('flow'), head=parsed.get('head'),
                     flow=parsed.get('flow') if parsed.get('head') else None,
                     power=parsed.get('voltage', 'unknown'),
                     power_options=[parsed['voltage']] if parsed.get('voltage') else [])
        if parsed.get('voltage') and parsed.get('phases') and (
                (parsed['voltage'] == '230' and parsed['phases'] != '1') or
                (parsed['voltage'] == '400' and parsed['phases'] != '3')):
            issues.append('Напряжение и число фаз требуют проверки исполнения двигателя.')
        facts['source'] = 'Характеристики карточки магазина: ' + '; '.join(notes)
    else:
        facts['source'] = 'Старый каталог: характеристики из текста; требуется заполнение карточки магазина.'
    # Explicit other duties in the name remain a rejection even if a generic
    # filtration attribute was accidentally assigned to an attraction pump.
    if re.search(r'гидромассаж|противоток|дренаж|фонтан|дозир|\bspa\b|\bспа\b', text(name)):
        issues.append('Название подтверждает другое назначение насоса.')
    facts['issue'] = ' '.join(dict.fromkeys(issues))[:1000]
    facts['source'] = facts['source'][:2000]
    specs['pump_facts'] = facts


def normalize(raw):
    from .pool_estimates import boolean, string
    if not isinstance(raw, dict) or not raw:
        return {}
    return {'in_category': boolean(raw.get('in_category', False)),
            'structured': boolean(raw.get('structured', False)),
            'prefilter': None if raw.get('prefilter') is None else boolean(raw['prefilter']),
            'issue': string(raw.get('issue', ''), 'Проверка насоса', 1000),
            'source': string(raw.get('source', ''), 'Источник характеристик насоса', 2000)}
