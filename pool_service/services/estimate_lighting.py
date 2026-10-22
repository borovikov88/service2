"""Pool lights: explicit quantities, catalog evidence, no inferred wiring design."""
import re
from decimal import Decimal

ROLES = {'pool_light': 'Прожектор бассейна', 'light_niche': 'Закладная прожектора',
         'light_transformer': 'Трансформатор освещения', 'light_box': 'Распределительная коробка освещения', 'light_controller': 'Контроллер RGB-освещения'}
OFF = 'Освещение отключено'
INCLUDED = 'Закладная входит в комплект прожектора'
RGB_OFF='Выбран белый свет — контроллер RGB не требуется'


def fountain_purpose(text):
    text=re.sub(r'\bне\s+(?:(?:подходит|предназначен\w*)\s+)?(?:для|под)\s+(?:фонтан\w*|fountains?)',' ',text or '',flags=re.I)
    return bool(re.search(r'фонтан|\bfountains?\b',text,re.I))


def normalize(raw):
    from .pool_estimates import string, number, boolean
    raw = {} if raw is None else raw
    if not isinstance(raw,dict):
        from .pool_estimates import EstimateInputError
        raise EstimateInputError('Некорректные характеристики освещения.')
    return {**{k: string(raw.get(k, ''), 'Характеристика освещения', 300) for k in ('material', 'models', 'supply', 'note', 'purpose', 'color')},
            **{k: str(number(raw[k], 'Характеристика освещения', minimum='0', maximum='100000', places=3)) if raw.get(k) is not None else None for k in ('watts', 'volts', 'leds')},
            **{k: boolean(raw.get(k, False)) for k in ('conflict', 'niche_included', 'denied','rgb_denied','white_denied')}}


def classify(specs, name, description, category, offer):
    text = name.casefold()
    scope = ' '.join((name, description, category)).casefold()
    kind = ('light_controller' if re.match(r'^(?:пульт|контроллер)\b',text) and re.search(r'RC[ -]?01\b',name,re.I) and re.search(r'прожектор|освещ',scope) else
            'light_niche' if re.match(r'^закладн\w*.*прожектор', text) else
            'light_transformer' if text.startswith('трансформатор') and re.search(r'освещ|прожектор|бассейн', scope) else
            'light_box' if re.match(r'^(?:дозов\w*|распределительн\w*|распаечн\w*) коробк', text) and re.search(r'освещ|прожектор|бассейн', scope) else
            'pool_light' if re.match(r'^прожектор\b', text) and re.search(r'освещ.*бассейн|для бассейн|подводн', scope) else '')
    if not kind: return
    params = ' '.join(f'{p.get("name", "")}: {p.text or ""}' for p in offer.findall('param'))
    from .estimate_embedded_candidates import without_hardware_grades, body_grade_claims, lining_claims
    evidence = without_hardware_grades(name) + '; ' + params
    materials = set()
    if re.search(r'\bABS\b|пластик', evidence, re.I): materials.add('plastic')
    materials.update('steel'+grade for grade in re.findall(r'\bAISI[ -]*(\d{3})',evidence,re.I))
    materials.update('steel'+grade for grade in body_grade_claims(description))
    if re.search(r'(?:материал корпуса|корпус)\s*[:—-]?\s*(?:из\s+)?(?:ABS|пластик)',description,re.I): materials.add('plastic')
    def one(pattern):
        values = {Decimal(x.replace(',', '.')) for x in re.findall(pattern, evidence, re.I)}
        return (str(next(iter(values))) if len(values)==1 else None), len(values)>1
    watts,wc=one(r'(\d+(?:[.,]\d+)?)\s*(?:Вт|W)\b')
    volts,vc=one(r'(\d+(?:[.,]\d+)?)\s*(?:В|V)\b')
    leds,lc=one(r'(\d+)\s*(?:LED|светодиод)')
    supplies = set(re.findall(r'\b(?:AC|DC)\b', evidence, re.I))
    included = bool(re.search(r'(?:\+|в комплекте|с)\s*закладн', name, re.I))
    niche_denied=bool(re.search(r'(?:без|не включает|не входит)\s+закладн|закладн\w*\s+(?:(?:деталь|часть)\s+)?не\s+(?:входит|включен\w*|поставля\w*)',scope))
    niche_conflict=included and niche_denied
    if niche_denied: included=False
    positive, denied_lining=lining_claims(name+' '+description+' '+params)
    specs['lining']=sorted(positive)
    specs['lining_denied']=sorted(denied_lining)
    denied = bool(re.search(r'(?:не подходит|не предназначен|не для)\s*(?:для\s+)?(?:бассейн|подводн)', scope))
    specs.update(kind=kind, embedded_role='')
    purpose='fountain' if fountain_purpose(scope) else ''
    color_scope=name+'; '+description+'; '+params
    rgb_denied=bool(re.search(r'(?:без|не\s+поддерживает|не)\s+RGB\b',color_scope,re.I))
    white_denied=bool(re.search(r'(?:без|не\s+поддерживает|не)\s+(?:White|бел\w*\s+свет)',color_scope,re.I))
    color_text=re.sub(r'(?:без|не\s+поддерживает|не)\s+(?:RGB|White|бел\w*\s+свет)\b',' ',evidence,flags=re.I)
    color_text=re.sub(r'(?:корпус|рамка|body|housing)\s*[:—-]?\s*White\b|\bWhite\s+(?:body|housing)\b',' ',color_text,flags=re.I)
    rgb=bool(re.search(r'\bRGB(?:W|X)?\b',color_text,re.I))
    white=bool(re.search(r'\b(?:White|NW|CW|WW)\b|бел(?:ый|ого)\s+(?:свет|свечени)|цвет\s+свечения\s*:\s*бел',color_text,re.I))
    specs['lighting'] = dict(rgb_denied=rgb_denied,white_denied=white_denied,purpose=purpose,color='rgb' if rgb else 'white' if white else '',material=next(iter(materials)) if len(materials)==1 else '', watts=watts, volts=volts, leds=leds,
        supply=next(iter(supplies)).upper() if len(supplies)==1 else '', models=' '.join(sorted(set(re.findall(r'\b(?:LED\d+|HT\d+[A-Z]*|SS\d+[A-Z]*)\b', evidence, re.I)))).upper(),
        conflict=(rgb and white) or (rgb and rgb_denied) or (white and white_denied) or niche_conflict or bool(positive & denied_lining) or len(materials)>1 or wc or vc or lc or len(supplies)>1, niche_included=included, denied=denied, note='Название и параметры карточки магазина; схема подключения требует проверки.')


def sync(snapshot, create=False):
    from .pool_estimates import make_row, EstimateInputError
    enabled = snapshot['inputs'].get('lighting') == 'yes'
    for variant in snapshot['variants'].values():
        block = next(b for b in variant['blocks'] if b['id']=='embedded')
        if enabled and create and not variant.get('lighting_initialized'):
            for kind, title in ROLES.items():
                if kind=='light_controller': continue
                if any(r['kind']==kind for b in variant['blocks'] for r in b['rows']): continue
                if len(block['rows'])>=100: raise EstimateInputError('Недостаточно места для освещения.')
                ids={r['id'] for b in variant['blocks'] for r in b['rows']}
                rid=next((f'{kind}_{i}' for i in range(100) if f'{kind}_{i}' not in ids), None)
                row=make_row(rid,title+': подобрать модель','шт.',snapshot['inputs'].get('light_count'),'lighting',kind)
                if kind in {'light_transformer','light_controller'}: row['quantity']='1'
                block['rows'].append(row)
            variant['lighting_initialized']=True
        if enabled and create and snapshot['inputs'].get('light_color')=='rgb' and not variant.get('light_controller_initialized'):
            if not any(r['kind']=='light_controller' for b in variant['blocks'] for r in b['rows']):
                if len(block['rows'])>=100: raise EstimateInputError('Недостаточно места для контроллера освещения.')
                ids={r['id'] for b in variant['blocks'] for r in b['rows']}
                rid=next(f'light_controller_{i}' for i in range(100) if f'light_controller_{i}' not in ids)
                block['rows'].append(make_row(rid,ROLES['light_controller']+': подобрать модель','шт.','1','lighting','light_controller'))
            variant['light_controller_initialized']=True
        lights=[r for r in block['rows'] if r['kind']=='pool_light' and not r['excluded']]
        for row in block['rows']:
            if row['kind'] not in ROLES or row['existing']: continue
            if not enabled and not row['excluded']: row.update(excluded=True, reason=OFF)
            elif enabled and row['excluded'] and row['reason']==OFF: row.update(excluded=False, reason='')
            if row['auto_quantity'] and row['unit']!='шт.': row['quantity']=None
            if row['auto_quantity'] and row['unit']=='шт.':
                row['quantity']='1' if row['kind'] in {'light_transformer','light_controller'} else snapshot['inputs'].get('light_count')
            if row['kind']=='light_controller' and enabled:
                if snapshot['inputs'].get('light_color')!='rgb' and not row['excluded']: row.update(excluded=True,reason=RGB_OFF)
                elif snapshot['inputs'].get('light_color')=='rgb' and row['excluded'] and row['reason']==RGB_OFF: row.update(excluded=False,reason='')
            if row['kind']=='light_niche' and len(lights)==1:
                included=(lights[0].get('catalog') or {}).get('lighting',{}).get('niche_included')
                if included and not row['excluded'] and row['auto_quantity'] and (row['price'] is None and not row.get('catalog') or row.get('catalog') and row['price']==row['catalog']['price']) and not row.get('selection_locked') and not row.get('unit_price_manual') and (row.get('catalog') or {}).get('selection')!='manual':
                    row.update(excluded=True, reason=INCLUDED)
                elif not included and row['excluded'] and row['reason']==INCLUDED: row.update(excluded=False,reason='')

        # Accessory quantities follow the actual single lamp row, including a
        # protected manual lamp count, never a stale parameter-form count.
        active=[r for r in block['rows'] if r['kind']=='pool_light' and not r['excluded']]
        for row in block['rows']:
            if row['kind'] in {'light_transformer','light_controller'} and row['auto_quantity'] and row['unit']=='шт.' and not row['existing']:
                row['quantity']='0' if not active else (None if any(r['quantity'] is None or r['unit']!='шт.' for r in active) else ('1' if any(Decimal(r['quantity'])>0 for r in active) else '0'))
            if row['kind']=='light_box' and row['auto_quantity'] and row['unit']=='шт.' and not row['existing']:
                row['quantity']=str(sum(Decimal(r['quantity']) for r in active)) if active and all(r['quantity'] is not None and r['unit']=='шт.' for r in active) else None
            if row['kind']=='light_niche' and row['auto_quantity'] and row['unit']=='шт.' and not row['existing']:
                row['quantity']=active[0]['quantity'] if len(active)==1 and active[0]['unit']=='шт.' else None


def assess(product, profile, parent=None, quantity=None, finish=None, color=None):
    f=product.get('lighting',{})
    if f.get('conflict') or product.get('specification_conflict'): return 'Противоречивые характеристики освещения.', []
    if f.get('purpose')=='fountain' or fountain_purpose(product.get('name','')): return 'Фонтанный светильник не подбирается как прожектор бассейна.', []
    if f.get('denied'): return 'Известен запрет использования в бассейне.', []
    notes=[]
    lining='film' if finish=='film' else 'concrete' if finish in {'tile','mosaic'} else None
    if lining in product.get('lining_denied',[]): return 'Запрещена выбранная отделка бассейна.', []
    if lining and product.get('lining') and lining not in product['lining']: return 'Товар указан для другой отделки бассейна.', []
    if product['kind']=='pool_light':
        if color and f.get(color+'_denied'): return 'Выбранный цвет освещения прямо исключён карточкой товара.', []
        if color and f.get('color') and f['color']!=color: return 'Цвет освещения не соответствует выбранному белому/RGB.', []
        if color and not f.get('color'): notes.append('Не подтверждён цвет освещения: '+color+'.')
        material='plastic' if profile.startswith('plastic') else profile
        if f.get('material') and f['material']!=material: return 'Материал прожектора не соответствует выбранному типу закладных.', []
        if not f.get('material'): notes.append('Не подтверждён материал корпуса.')
        if f.get('volts') and Decimal(f['volts'])>24: return 'Не подтверждена безопасная схема подводного освещения с этим напряжением.', []
        for field,label in (('watts','мощность'),('volts','напряжение'),('supply','AC/DC'),('leds','число светодиодов')):
            if not f.get(field): notes.append('Не указано: '+label+'.')
    if product['kind']=='light_controller':
        notes.append('Проверить протокол контроллера, подключение и допустимую нагрузку с выбранными RGB-прожекторами.')
    if product['kind']=='light_niche':
        pf=(parent or {}).get('lighting',{})
        if f.get('models') and pf.get('models') and not set(f['models'].split())&set(pf['models'].split()): return 'Закладная указана для другой серии прожектора.', []
        if not f.get('models') or not pf.get('models'): notes.append('Совместимость закладной с серией прожектора не подтверждена.')
    if product['kind']=='light_transformer':
        pf=(parent or {}).get('lighting',{})
        for field in ('volts','supply'):
            if f.get(field) and pf.get(field) and (Decimal(f[field])!=Decimal(pf[field]) if field=='volts' else f[field]!=pf[field]): return 'Выход трансформатора не соответствует прожектору.', []
            if not f.get(field) or not pf.get(field): notes.append('Проверить выход трансформатора: '+field+'.')
        if f.get('watts') and pf.get('watts') and quantity is not None:
            if Decimal(f['watts'])<Decimal(pf['watts'])*Decimal(quantity): return 'Мощность трансформатора ниже суммарной мощности прожекторов.', []
        else: notes.append('Недостаточно данных для проверки суммарной мощности.')
        notes.append('Запас мощности, кабель, защита и безопасное разделение цепей требуют проекта.')
    notes.append('Проверить отделку, комплектность и схему монтажа освещения.')
    return '', notes


def fill(snapshot, catalog, only=None):
    from .estimate_catalog import apply_product, select_price_tier, refresh_auto_facts
    sync(snapshot, create=True)
    count=0
    for tier,v in snapshot['variants'].items():
        if only and only!=tier: continue
        block=next(b for b in v['blocks'] if b['id']=='embedded')
        if block['excluded']: continue
        parent=None
        light_rows=[r for r in block['rows'] if r['kind']=='pool_light' and not r['excluded']]
        light_quantity=light_rows[0]['quantity'] if len(light_rows)==1 else None
        for kind in ROLES:
            for row in block['rows']:
                if row['kind']!=kind or row['excluded'] or row['existing']: continue
                origin=row.get('catalog')
                if kind=='pool_light' and origin: parent=origin
                if row['unit']!='шт.' or not row['auto_quantity'] or row.get('selection_locked') or row.get('unit_price_manual') or origin and (origin['selection']!='auto' or Decimal(row['price'] or '-1')!=Decimal(origin['price'])) or not origin and (row['price'] is not None or row['model']): continue
                if kind!='pool_light' and len(light_rows)!=1:
                    row['selection_note']='Проверить: несколько прожекторов разных моделей; нужна явная схема связей.'
                    continue
                choices=[]
                for p in catalog['products'].values():
                    if p['kind']!=kind or not p['available']: continue
                    issue,notes=assess(p,v['embedded_profile'],parent,light_quantity,snapshot['inputs']['finish'],snapshot['inputs'].get('light_color'))
                    if not issue: choices.append((p,notes))
                if not choices:
                    if origin and origin['selection']=='auto':
                        current=catalog['products'].get(origin['id']) or {**origin,'kind':kind}
                        issue,_=assess(current,v['embedded_profile'],parent,light_quantity,snapshot['inputs']['finish'],snapshot['inputs'].get('light_color'))
                        if issue: row.update(catalog=None, price=None, model='', name=ROLES[kind]+': подобрать модель')
                    row['selection_note']='Проверить: подходящий '+ROLES[kind].lower()+' в каталоге не найден.'
                    continue
                best=min(len(n) for p,n in choices)
                choices=[(p,n) for p,n in choices if len(n)==best]
                if kind=='pool_light' and tier=='standard':
                    known=[p for p,n in choices if p.get('lighting',{}).get('leds')]
                    if known:
                        maximum=max(Decimal(p['lighting']['leds']) for p in known)
                        choices=[(p,n) for p,n in choices if p.get('lighting',{}).get('leds') and Decimal(p['lighting']['leds'])==maximum]
                product=select_price_tier([p for p,n in choices],tier)
                notes=next(n for p,n in choices if p['id']==product['id'])
                if origin and origin['id']==product['id']: refresh_auto_facts(row,product,catalog)
                else: apply_product(row,product,catalog,'auto'); count+=1
                row['selection_note']='Проверить: '+' '.join(notes)
                if kind=='pool_light': parent=row['catalog']
            sync(snapshot)
    return count


def annotate(snapshot, variant):
    physical=[r for b in variant['blocks'] for r in b['rows'] if not b['excluded'] and not r['excluded']]
    lights=[r for r in physical if r['kind']=='pool_light']
    parent=lights[0].get('catalog') if len(lights)==1 else None
    quantity=lights[0]['quantity'] if len(lights)==1 and lights[0]['unit']=='шт.' else None
    for row in physical:
        if row['kind'] not in ROLES: continue
        issue,notes=assess({**(row.get('catalog') or {}),'kind':row['kind'],'name':row['name']},variant['embedded_profile'],parent,quantity,snapshot['inputs']['finish'],snapshot['inputs'].get('light_color'))
        if len(lights)!=1: notes.append('Для нескольких моделей нужна явная схема связей; автоматическая совместимость не подтверждена.')
        if row['quantity'] is None: notes.append('Укажите количество по схеме размещения.')
        if row['unit']!='шт.': notes.append('Расчёт количества освещения требует единицы шт.; текущая единица сохранена.')
        if row['kind']=='light_niche' and (parent or {}).get('lighting',{}).get('niche_included'): notes.append('Закладная уже входит в прожектор; проверьте необходимость отдельной ручной позиции.')
        row['compatibility']={'message':'Проверить: '+ ' '.join(([issue] if issue else [])+notes),'can_auto':False}
