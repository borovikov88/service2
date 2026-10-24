"""Public shop nozzle taxonomy; no guessed materials or hydraulic performance."""
from copy import deepcopy
import re
import xml.etree.ElementTree as ET
from .estimate_catalog_params import text, grade, apply as apply_params

TYPES = {
    'возвратная форсунка': 'inlet', 'стеновая форсунка': 'inlet',
    'пылесосная форсунка': 'vacuum', 'форсунка для бассейна': 'unknown',
    'гидромассажная форсунка': 'blocked', 'воздушная форсунка': 'blocked',
    'всасывающая форсунка': 'blocked', 'донная форсунка': 'blocked',
    'фонтанная форсунка': 'blocked', 'форсунка для фонтана': 'blocked',
    'резьбовая заглушка': 'blocked', 'слив переливного лотка': 'blocked',
    'клапан для пылесосной форсунки': 'blocked', 'шумогаситель': 'blocked',
    'водозабор': 'blocked', 'вставка форсунки': 'blocked', 'накладка форсунки': 'blocked',
    'штуцер для шланга пылесоса': 'blocked', 'стеновой проход': 'blocked',
    'фланцевый комплект': 'blocked', 'контргайка с прокладкой': 'blocked',
}


def classify(specs, name, description, category, url, offer):
    params = {}
    for p in offer.findall('param'):
        params.setdefault(text(p.get('name')), []).append(text(p.text))
    title = text(name)
    if not (specs.get('embedded_role') in {'inlet','vacuum'} or 'форсунк' in title
            or 'форсунк' in text(category) or any(v in TYPES for v in params.get('тип товара', []))):
        return
    notes, issue = [], ''
    roles = {TYPES.get(v, 'unknown') for v in params.get('тип товара', [])}
    if roles:
        notes.append('Тип товара: ' + '; '.join(params['тип товара']))
        if 'blocked' in roles:
            issue = 'Тип товара не является возвратной стеновой форсункой: ' + '; '.join(params['тип товара']) + '.'
        elif len(roles) > 1:
            issue = 'Противоречивый тип товара в характеристиках магазина.'
        elif next(iter(roles)) in {'inlet','vacuum'}:
            role=next(iter(roles))
            if (specs.get('embedded_role') == 'vacuum' and role == 'inlet' or role == 'vacuum' and re.match(r'^(?:возвратная|стеновая)\s+форсунка', title)):
                issue='Название и тип товара противоречат друг другу: возвратная и пылесосная форсунки.'
            else:
                specs['embedded_role'] = role
    # Known other duties override a generic noun or an incorrectly labelled return type.
    clean = text(name + '. ' + description)
    clean = re.sub(r'\bне\s+(?:(?:предназначен\w*|подходит)\s+)?(?:для|под)\s+(?:фонтан|гидромассаж|всасыван)\w*', ' ', clean)
    known_wrong = re.search(r'фонтанн\w*\s+форсунк|форсунк\w*\s+(?:для\s+)?фонтан|(?:для|назначение\s*:)\s*(?:фонтан|гидромассаж|всасыван)\w*|гидромассажн\w*\s+форсунк|воздушн\w*\s+форсунк|всасывающ\w*\s+форсунк|донн\w*\s+форсунк', clean)
    fountain_path = re.search(r'фонтан|fountain|/fontany(?:/|\.html)|dlya-fontan', text(category + ' ' + url))
    if known_wrong or fountain_path:
        issue = 'Подтверждено иное назначение форсунки (фонтан, гидромассаж, всасывание или донное размещение); для стенового возврата воды не подбирается.'
        notes.append('Источник назначения: название/описание/категория товара')
    placements = {x.strip() for v in params.get('размещение', []) for x in re.split(r'[;,/]', v)}
    if placements:
        notes.append('Размещение: ' + '; '.join(sorted(placements)))
        if 'стеновое' not in placements and placements <= {'донное','в переливном лотке'}:
            issue = 'В характеристиках указано донное размещение или переливной лоток, требуется стеновая форсунка.'
    # Map only the exact public shop aliases, only for this family. Existing
    # contradiction detection remains authoritative, including upstream conflicts.
    mapped = deepcopy(offer)
    for alias, target in (('материал','Материал корпуса'),('покрытие чаши','Тип отделки')):
        for value in params.get(alias, []):
            if alias == 'материал' and value in {'бронза','латунь'}:
                specs['body_family'] = 'other_metal'
            elif alias == 'покрытие чаши' and 'фланцев' in value:
                issue = 'Для выбранного покрытия требуется отдельный фланцевый комплект; его наличие не подтверждено: ' + value + '.'
            else:
                ET.SubElement(mapped,'param',{'name':target}).text = value
            notes.append(alias + ': ' + value)
    for value in params.get('марка материала', []):
        if grade(value):
            ET.SubElement(mapped,'param',{'name':'Марка стали корпуса'}).text = value
        elif value in {'abs','абс','abs-пластик'}:
            ET.SubElement(mapped,'param',{'name':'Материал корпуса'}).text = value
        notes.append('Марка материала: ' + value)
    if len(mapped) != len(offer):
        apply_params(specs,mapped)
    # New title order is common in the cleaned shop category.
    if not specs.get('embedded_role') and re.match(r'^(?:возвратная|стеновая)\s+форсунка\b',title):
        specs['embedded_role']='inlet'
    if not specs.get('embedded_role') and (issue or roles == {'unknown'}):
        specs['embedded_role']='inlet'  # searchable manually, always assessed below
    specs['inlet_taxonomy'] = {'issue':issue[:500], 'source':'; '.join(notes)[:1000],
                              'type':'; '.join(params.get('тип товара', []))[:200]}


def normalize(raw):
    from .pool_estimates import string
    raw = raw if isinstance(raw,dict) else {}
    return {k:string(raw.get(k,''),'Характеристика форсунки',limit) for k,limit in
            (('issue',500),('source',1000),('type',200))}
