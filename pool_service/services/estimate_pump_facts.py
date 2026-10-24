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
    # Exact public card wording, verified against the production catalogue.
    if value in {
        'фильтрация и циркуляция воды в бассейне',
        'циркуляция воды в фильтровальной системе бассейна',
        'циркуляция воды в системе фильтрации бассейна',
        'циркуляция и фильтрация общественных бассейнов',
        'фильтрация и циркуляция воды в общественных бассейнах',
        'циркуляция воды и фильтрация бассейна',
        'циркуляция и фильтрация воды бассейна',
    }:
        return ('фильтрация', 'циркуляция')
    if value in {'циркуляция воды плавательного бассейна', 'циркуляция воды бассейна',
                 'циркуляция воды в бассейне', 'циркуляция воды в небольших частных и наземных бассейнах'}:
        return ('циркуляция',)
    values = set(re.split(r'\s*[;,/]\s*', value))
    known = {'фильтрация', 'циркуляция', 'гидромассаж', 'противоток', 'аттракционы',
             'фонтан', 'фонтаны', 'дренаж', 'дозирование', 'спа', 'spa'}
    return tuple(sorted(values)) if values and values <= known else None


def frequency(value):
    values = re.split(r'\s*/\s*', re.sub(r'\s*(?:гц|hz)$', '', value))
    return tuple(sorted(set(values))) if values and set(values) <= {'50', '60'} else None


def classify(specs, name, description, category, url, offer):
    params = {}
    for param in offer.findall('param'):
        params.setdefault(text(param.get('name')), []).append(text(param.text))
    markers = params.get('категория подбора', [])
    ancestry = [text(part) for part in category.split(' / ')]
    in_category = (set(markers) == {CATEGORY} if markers else
                   ancestry[:3] == ['бассейны', 'оборудование для бассейнов', CATEGORY])
    pump_types = {'насос', 'центробежный насос с префильтром'}
    if specs['kind'] != 'pump' and not (in_category and set(params.get('тип товара', [])) <= pump_types and params.get('тип товара')):
        return
    # A known heater/primary-circuit/accessory identity cannot be relabelled by
    # a category marker or a generic public type alone.
    if specs['kind'] not in {'pump', 'other'}:
        return
    issues = []
    structured = any(params.get(label) for label in FIELDS.values())
    facts = {'in_category': in_category, 'structured': structured,
             'prefilter': None, 'issue': '', 'source': '', 'mode_note': '', 'purpose_note': ''}
    notes = []
    if not in_category:
        issues.append('Принадлежность категории «Насосы для бассейнов» не подтверждена.')
    if structured:
        parsers = {
            'type': lambda v: 'насос' if v in pump_types else None,
            'purpose': purpose,
            'prefilter': lambda v: {'да': True, 'нет': False}.get(v),
            'voltage': voltage,
            'flow': lambda v: number(v, r'(?:м[³3]/(?:ч|час)|m[³3]/h)'),
            'head': lambda v: number(v, r'[мm]', '1000'),
            'phases': lambda v: v if v in {'1', '3'} else None,
            'frequency': frequency,
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
        provisional_circulation = duties == ('циркуляция',) and parsed.get('prefilter') is True
        if provisional_circulation:
            facts['purpose_note'] = 'назначение для фильтрации: карточка подтверждает циркуляцию воды бассейна и префильтр'
        elif duties and ('фильтрация' not in duties or set(duties) - {'фильтрация', 'циркуляция'}):
            issues.append('Назначение насоса не подтверждает обычную фильтрацию бассейна: ' + '; '.join(duties) + '.')
        facts['prefilter'] = parsed.get('prefilter')
        # Scalar card fields already explicitly link head to the stated flow.
        # Check additional numeric claims without imposing one prose template.
        modes = params.get(FIELDS['mode'], [])
        for value in modes:
            notes.append(FIELDS['mode'] + ': ' + value)
            claims = {
                'flow': [number(v) for v in re.findall(r'(\d+(?:[.,]\d+)?)\s*м[³3]/ч', value)],
                'head': [number(v) for v in re.findall(r'при\s+(?:напоре\s+)?(\d+(?:[.,]\d+)?)\s*м\b', value)],
                'voltage': [voltage(v) for v in re.findall(r'(?<!\d)(220|230|380|400)\s*в\b', value)],
            }
            if any(parsed.get(k) is not None and any(v != parsed[k] for v in values) for k, values in claims.items()):
                issues.append('Режим указанной производительности противоречит расходу, напору или питанию карточки.')
            if re.search(r'q\s*max|h\s*max|максимальн', value):
                facts['mode_note'] = 'рабочая Q–H точка вместо максимальных значений режима'
            elif re.search(r'об/мин|двухскорост|исходного наименования', value):
                facts['mode_note'] = 'режим и скорость насоса на объекте: ' + value
            elif not any(claims.values()) and value not in {'рабочая каталожная точка поставщика', 'заявленная производительность'}:
                facts['mode_note'] = 'условия указанного режима насоса: ' + value
        specs.update(kind='pump', filtration='фильтрация' in duties or provisional_circulation,
                     nominal_flow=parsed.get('flow'), head=parsed.get('head'),
                     flow=parsed.get('flow') if parsed.get('head') else None,
                     power=parsed.get('voltage', 'unknown'),
                     power_options=[parsed['voltage']] if parsed.get('voltage') else [])
        if facts['mode_note'].startswith('рабочая Q–H точка вместо'):
            specs.update(flow=None, head=None)
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
    facts['mode_note'] = facts['mode_note'][:500]
    specs['pump_facts'] = facts


def normalize(raw):
    from .pool_estimates import boolean, string
    if not isinstance(raw, dict) or not raw:
        return {}
    return {'in_category': boolean(raw.get('in_category', False)),
            'structured': boolean(raw.get('structured', False)),
            'prefilter': None if raw.get('prefilter') is None else boolean(raw['prefilter']),
            'issue': string(raw.get('issue', ''), 'Проверка насоса', 1000),
            'mode_note': string(raw.get('mode_note', ''), 'Режим насоса', 500),
            'purpose_note': string(raw.get('purpose_note', ''), 'Назначение насоса', 500),
            'source': string(raw.get('source', ''), 'Источник характеристик насоса', 2000)}
