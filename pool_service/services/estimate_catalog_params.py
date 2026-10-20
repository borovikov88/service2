"""Conservative structured shop facts. Never infer body material from hardware.

Aliases are explicit: arbitrary marketing attributes are not engineering evidence.
Repeated contradictory/unknown values block the affected fact instead of last-wins.
"""
import re
from html import unescape

ALIASES = {
    'body': {'материал корпуса', 'body material'},
    'grade': {'марка стали корпуса', 'марка стали', 'body steel grade'},
    'lining': {'тип отделки', 'тип покрытия бассейна', 'отделка бассейна', 'pool lining'},
    'builtin': {'регулятор уровня в комплекте', 'встроенный регулятор уровня', 'built-in level regulator'},
    'scheme': {'схема регулятора уровня', 'level regulator scheme'},
    'purpose': {'назначение линии уровня', 'level line purpose'},
    'passage': {'свободный проход линии уровня', 'level line free passage'},
    'connection': {'тип подключения линии уровня', 'level line connection type'},
    'diameter': {'диаметр подключения линии уровня', 'level line diameter'},
    'regulators': {'совместимые регуляторы уровня (артикулы)', 'compatible level regulator skus'},
}


def text(value):
    return ' '.join(unescape(re.sub(r'<[^>]*>', ' ', value or '')).casefold().replace('ё', 'е').split())


def body(value):
    if value in {'abs', 'абс', 'пластик', 'abs-пластик', 'abs пластик', 'пластик abs'}:
        return 'plastic'
    if value in {'нержавеющая сталь', 'нержавейка', 'stainless steel'}:
        return 'steel'
    return grade(value)


def grade(value):
    m = re.fullmatch(r'(?:нержавеющая сталь\s+)?(?:aisi\s*[- ]?\s*)?(304|316)[lл]?', value)
    return 'steel' + m[1] if m else None


def lining(value):
    tokens = re.split(r'\s*(?:[,;/+]|\s+и\s+)\s*', value)
    options = {'пленка':'film', 'под пленку':'film', 'лайнер':'film', 'liner':'film', 'film':'film',
               'плитка':'concrete', 'под плитку':'concrete', 'бетон':'concrete', 'мозаика':'concrete', 'tile':'concrete', 'concrete':'concrete'}
    return tuple(sorted({options[t] for t in tokens})) if tokens and all(t in options for t in tokens) else None


def builtin(value):
    if value in {'да', 'есть', 'в комплекте', 'yes', 'true'}:
        return 'included'
    if value in {'нет', 'отсутствует', 'не входит в комплект', 'no', 'false'}:
        return 'absent'
    return None


def scheme(value):
    return 'external_float' if value in {'внешний поплавковый через отдельную форсунку', 'external_float'} else None


def regulators(value):
    tokens = re.split(r'\s*[,;]\s*', value)
    return tuple(sorted(set(tokens))) if 0 < len(tokens) <= 30 and all(re.fullmatch(r'\d{1,12}', t) for t in tokens) else None


# These describe the dedicated communicating line, not the mains-water inlet.
HYDRAULICS = {
    'purpose': {'отдельная сообщающаяся линия': 'communicating', 'communicating': 'communicating',
                'напорная линия фильтрации': 'filtration', 'filtration': 'filtration'},
    'passage': {'да': 'yes', 'yes': 'yes', 'нет': 'no', 'no': 'no'},
    'connection': {'клеевое внутреннее': 'glue_socket', 'glue_socket': 'glue_socket',
                   'клеевое наружное': 'glue_spigot', 'glue_spigot': 'glue_spigot',
                   'bsp внутренняя': 'bsp_female', 'bsp_female': 'bsp_female',
                   'bsp наружная': 'bsp_male', 'bsp_male': 'bsp_male'},
}


def diameter(value):
    # Explicit units only: DN50, 50 mm and G1½ are not interchangeable evidence.
    m = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*(мм|mm|дюйм|inch)', value)
    if not m:
        return None
    from decimal import Decimal
    number = Decimal(m[1].replace(',', '.'))
    if not 0 < number <= 1000:
        return None
    return format(number.normalize(), 'f') + (' mm' if m[2] in {'мм','mm'} else ' inch')


def apply(specs, offer):
    parsers = {'body': body, 'grade': grade, 'lining': lining, 'builtin': builtin, 'scheme': scheme, 'regulators': regulators}
    parsers.update({key: (lambda value, options=options: options.get(value)) for key, options in HYDRAULICS.items()})
    parsers['diameter'] = diameter
    facts, invalid, contradictions = {}, set(), set()
    for key, aliases in ALIASES.items():
        values = [parsers[key](text(p.text)) for p in offer.findall('param') if text(p.get('name')) in aliases]
        if values:
            if None in values or len(set(values)) != 1:
                invalid.add(key)
                if len(set(values) - {None}) > 1:
                    contradictions.add(key)
            else:
                facts[key] = values[0]
    problems = []
    material = facts.get('body')
    steel = facts.get('grade')
    if steel and material == 'steel':
        material = steel
    elif steel and material and material != steel:
        invalid.add('body')
    elif steel and not material:
        # A bare steel-grade attribute may describe screws, not the body.
        if (specs.get('material', '').startswith('steel') or any(
                text(p.get('name')) in {'марка стали корпуса', 'body steel grade'}
                for p in offer.findall('param'))):
            material = steel
    old = specs.get('material', '')
    if material == 'steel':
        material = old if old.startswith('steel') else ''
        if old == 'plastic': invalid.add('body')
    if material and old and material != old:
        invalid.add('body')
    if invalid & {'body', 'grade'}:
        specs['material'] = ''
        problems.append('Материал корпуса: неизвестное значение или конфликт источников.')
    elif material:
        specs['material'] = material
    if 'lining' in facts and specs.get('lining') and set(facts['lining']) != set(specs['lining']):
        invalid.add('lining')
    if 'lining' in invalid:
        specs['lining'] = []
        problems.append('Отделка: неизвестное значение или конфликт источников.')
    elif 'lining' in facts:
        specs['lining'] = list(facts['lining'])
    specs.setdefault('level_scheme', '')
    specs.setdefault('level_regulator_skus', [])
    for key, target, default in [('builtin', 'level_control', 'unknown'), ('scheme', 'level_scheme', ''), ('regulators', 'level_regulator_skus', [])]:
        if key in invalid:
            specs[target] = default
            problems.append('Не подтверждена характеристика XML: ' + key + '.')
        elif key in facts:
            specs[target] = list(facts[key]) if key == 'regulators' else facts[key]
    for key in ('purpose', 'passage', 'connection', 'diameter'):
        specs['level_' + key] = facts.get(key, '')
    hydraulic_keys = {'purpose', 'passage', 'connection', 'diameter'}
    specs['level_data_issue'] = 'conflict' if contradictions & hydraulic_keys else 'unknown' if invalid & hydraulic_keys else ''
    specs['specification_conflict'] = bool(problems)
    if facts or invalid:
        labels = {'purpose': 'назначение линии уровня', 'passage': 'свободный проход линии уровня',
                  'connection': 'тип подключения линии уровня', 'diameter': 'диаметр подключения линии уровня'}
        hydraulic_note = (' Уточните поля: ' + ', '.join(labels[k] for k in sorted(invalid & hydraulic_keys)) + '.') if invalid & hydraulic_keys else ''
        note = 'Характеристики XML param. ' + ' '.join(problems) + hydraulic_note
        specs['specification_note'] = (specs.get('specification_note', '') + ' ' + note).strip()[:1000]
