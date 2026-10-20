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


def apply(specs, offer):
    parsers = {'body': body, 'grade': grade, 'lining': lining, 'builtin': builtin, 'scheme': scheme, 'regulators': regulators}
    facts, invalid = {}, set()
    for key, aliases in ALIASES.items():
        values = [parsers[key](text(p.text)) for p in offer.findall('param') if text(p.get('name')) in aliases]
        if values:
            if None in values or len(set(values)) != 1:
                invalid.add(key)
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
    specs['specification_conflict'] = bool(problems)
    if facts or invalid:
        note = 'Характеристики XML param. ' + ' '.join(problems)
        specs['specification_note'] = (specs.get('specification_note', '') + ' ' + note).strip()[:1000]
