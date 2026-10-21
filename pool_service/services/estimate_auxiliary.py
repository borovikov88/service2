"""Purpose-specific dosing and filtration controls; no invented hydraulic facts."""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from html import unescape
import re

TITLES = {'treatment': 'Обеззараживание: подобрать комплект',
          'control_panel': 'Панель управления фильтрацией: подобрать модель'}
MODES = {'', 'semi_chlorine', 'rx_ph', 'cl_ph', 'ph_only', 'rx_only', 'unknown'}
NUMBERS = ('max_volume', 'max_kw', 'max_current', 'pump_count', 'motor_kw', 'motor_current')


def treatment_title(tier):
    return {'economy':'Полуавтоматический дозатор хлора: подобрать модель',
            'standard':'Станция дозирования Rx + pH: подобрать модель',
            'premium':'Станция дозирования Cl + pH: подобрать модель'}[tier]


def normalize_facts(raw):
    from .pool_estimates import EstimateInputError
    if raw is None: raw = {}
    if not isinstance(raw, dict): raise EstimateInputError('Некорректные характеристики дозирования/панели.')
    result = {'mode':raw.get('mode',''), 'rx':raw.get('rx',False), 'free_cl':raw.get('free_cl',False),
              'incomplete':raw.get('incomplete',False), 'conflict':raw.get('conflict',False),
              'wrong_purpose':raw.get('wrong_purpose',False)}
    if result['mode'] not in MODES or any(type(result[k]) is not bool for k in ('rx','free_cl','incomplete','conflict','wrong_purpose')):
        raise EstimateInputError('Некорректное назначение дозирования/панели.')
    for key in NUMBERS:
        value=raw.get(key)
        if value is None: result[key]=None; continue
        try:
            if isinstance(value,bool): raise ValueError
            number=Decimal(str(value))
            if not number.is_finite() or number <= 0 or number > 100000: raise ValueError
            if key=='pump_count' and number != number.to_integral_value(): raise ValueError
        except (InvalidOperation,ValueError,TypeError):
            raise EstimateInputError('Некорректный предел оборудования: '+key) from None
        result[key]=str(number)
    return result


def classify(specs, name, description='', offer=None):
    title=unescape(name).casefold().replace('ё','е').replace('рн','ph').replace('рх','rx')
    desc=unescape(re.sub(r'<[^>]*>',' ',description or '')).casefold().replace('ё','е')
    facts=normalize_facts({})
    # Identity from a complete device's title, never accessories or a broad category.
    accessory=bool(re.match(r'^(?:датчик|электрод|ячейка|запчаст|ремкомплект|крышка|тестер|реагент|набор\s+(?:датчик|электрод))',title))
    semi=bool(re.match(r'^(?:хлоратор[ -]*полуавтомат|полуавтоматическ\w*\s+(?:дозатор\s+хлора|хлоратор)|дозатор\s+хлора\s+полуавтомат)',title))
    station=bool(re.match(r'^(?:(?:автоматическ\w*\s+)?(?:станция\s+(?:автоматическ\w*\s+)?дозирован|дозирующ\w*\s+станция)|система\s+дозирующих\s+насосов)',title))
    panel=bool(re.match(r'^(?:панель|шкаф|щит)\s+управления\s+фильтрац',title))
    if not accessory and (semi or station):
        specs['kind']='treatment'
        ph=bool(re.search(r'(?<![a-z])ph(?![a-z])',title))
        rx=bool(re.search(r'(?<![a-z])(?:rx|orp|redox)(?![a-z])|редокс',title))
        cl=bool(re.search(r'(?<![a-z])cl(?![a-z])|свободн\w*\s+хлор',title))
        facts.update(rx=rx,free_cl=bool(re.search(r'free\s*cl|свободн\w*\s+хлор',title)))
        facts['mode']='semi_chlorine' if semi else 'cl_ph' if ph and cl else 'rx_ph' if ph and rx else 'ph_only' if ph else 'rx_only' if rx else 'unknown'
        facts['incomplete']=bool(re.search(r'без\s+(?:дозирующ\w*\s+)?насос|без\s+(?:измерительн\w*\s+)?(?:датчик|электрод|набор)',title+' '+desc))
    elif not accessory and panel:
        specs['kind']='control_panel'
    elif specs.get('kind')!='pump':
        specs['auxiliary']=facts
        return

    text=title+' '+desc
    for match in re.finditer(r'(?:для|фильтраци[ия])\s+(?:питьевой\s+воды|котлов|систем\s+отопления|сточных\s+вод)',text):
        if re.search(r'\bне\s+(?:(?:подходит|предназначен\w*)\s+)?$',text[:match.start()]): continue
        denial=re.match(r'\s*[,—:-]?\s*не\s+(?:подходит|предназначен\w*)\b',text[match.end():])
        if denial and not re.match(r'\s+для\b',text[match.end()+denial.end():]): continue
        facts['wrong_purpose']=True

    def numbers(key, pattern, text):
        values={Decimal(v.replace(',','.')) for v in re.findall(pattern,text)}
        if any(v <= 0 for v in values): facts['conflict']=True
        values={v for v in values if v > 0}
        if len(values)>1: facts['conflict']=True
        if len(values)==1: facts[key]=str(next(iter(values)))
    n=r'(\d+(?:[.,]\d+)?)'
    if specs['kind']=='treatment':
        numbers('max_volume',r'(?:бассейн\w*(?:\s+объем\w*)?\s+до|объем\s+бассейна\s*[:—-]?\s*(?:до)?)\s*'+n+r'\s*м[³3]',title+' '+desc)
    if specs['kind']=='control_panel':
        numbers('max_kw',r'(?:мощность\s+насоса\s*[:—-]?\s*(?:до)?|насос\w*\s+до)\s*'+n+r'\s*(?:квт|kw)',title+' '+desc)
        numbers('max_current',n+r'\s*(?:а|a)\b',title)
        counts=set(re.findall(r'(\d+)\s+насос',title))
        if 'два насоса' in title: counts.add('2')
        if 'один насос' in title: counts.add('1')
        if len(counts)>1: facts['conflict']=True
        if len(counts)==1: facts['pump_count']=next(iter(counts))
    if specs['kind']=='pump':
        numbers('motor_kw',n+r'\s*(?:квт|kw)\b',title)
        numbers('motor_current',n+r'\s*(?:а|a)\b',title)
    # Only exact parameter labels: unrelated limits must not become motor ratings.
    labels={'максимальный объем бассейна':('max_volume','м3'),
            'максимальная мощность насоса':('max_kw','квт'),
            'максимальный ток насоса':('max_current','а'),
            'количество насосов':('pump_count',''),
            'мощность двигателя':('motor_kw','квт'),
            'номинальный ток двигателя':('motor_current','а')}
    if offer is not None:
        for param in offer.findall('param'):
            label=(param.get('name') or '').casefold().replace('ё','е').strip()
            text=(param.text or '').casefold().strip().replace('рн','ph').replace('рх','rx')
            if label in {'измеряемые параметры','контролируемые параметры'} and specs['kind']=='treatment':
                ph=bool(re.search(r'(?<![a-z])ph(?![a-z])',text))
                rx=bool(re.search(r'(?<![a-z])(?:rx|orp|redox)(?![a-z])|редокс|овп',text))
                cl=bool(re.search(r'(?<![a-z])cl(?![a-z])|хлор',text))
                mode='cl_ph' if ph and cl else 'rx_ph' if ph and rx else 'ph_only' if ph else 'rx_only' if rx else 'unknown'
                if mode!='unknown':
                    if facts['mode'] not in ('', 'unknown',mode): facts['conflict']=True
                    else: facts.update(mode=mode,rx=rx or facts['rx'],free_cl=facts['free_cl'] or bool(re.search(r'free\s*cl|свободн\w*\s+хлор',text)))
                continue
            if label in {'напряжение питания','питание'} and specs['kind'] in TITLES:
                voltages={('230' if v in {'220','230'} else '400') for v in re.findall(r'(?<!\d)(220|230|380|400)\s*(?:в|v)?\b',text)}
                if voltages:
                    previous=set(specs.get('power_options') or [])
                    if previous and previous!=voltages: facts['conflict']=True
                    else:
                        specs['power_options']=sorted(voltages)
                        specs['power']=next(iter(voltages)) if len(voltages)==1 else 'unknown'
                continue
            if label not in labels: continue
            key,unit=labels[label]
            def canonical_unit(value):
                value=value.casefold().strip().replace('³','3')
                return {'kw':'квт','a':'а','m3':'м3','шт':'','шт.':''}.get(value,value)
            text=(param.text or '').casefold().strip().replace('³','3')
            declared=canonical_unit(param.get('unit') or '')
            # XML often separates the numeric text from its unit attribute.
            # Conflicting declared dimensions must never become unknown limits.
            if param.get('unit') and declared != unit:
                facts['conflict']=True
                continue
            if re.fullmatch(n,text) and (declared == unit or not unit):
                text += unit
            text=re.sub(r'(kw|m3|a)$',lambda m:canonical_unit(m.group()),text)
            match=re.fullmatch(n+r'\s*'+re.escape(unit),text)
            if not match: continue
            value=str(Decimal(match.group(1).replace(',','.')))
            if facts[key] is not None and Decimal(facts[key])!=Decimal(value): facts['conflict']=True
            elif Decimal(value)>0: facts[key]=value
    specs['auxiliary']=facts


def assess(product, snapshot, variant, tier):
    facts=product.get('auxiliary') or {}
    if not product.get('available'): return 'Доступность товара не подтверждена.',[]
    if facts.get('wrong_purpose'): return 'Известное иное назначение оборудования; применение в бассейне не подтверждено.',[]
    if product.get('specification_conflict') or facts.get('conflict'):
        return 'Противоречивые характеристики оборудования; проверьте источник.',[]
    missing=[]
    if product['kind']=='treatment':
        mode=facts.get('mode','')
        desired={'economy':'semi_chlorine','standard':'rx_ph','premium':'cl_ph'}[tier]
        if tier=='economy' and mode!='semi_chlorine':
            return 'Для эконом требуется полуавтоматический дозатор хлора, не автоматическая станция.',[]
        if mode not in ('', 'unknown',desired) and not (tier=='standard' and mode=='cl_ph' and facts.get('rx')):
            return 'Тип дозирования не соответствует КП: требуется '+treatment_title(tier).split(':')[0]+'.',[]
        if facts.get('incomplete'): return 'Указана неполная станция без насосов/измерительного комплекта; комплект не подтверждён.',[]
        if mode in ('','unknown'): missing.append('состав станции и метод измерения '+('Cl + pH' if tier=='premium' else 'Rx + pH' if tier=='standard' else 'полуавтоматического дозатора хлора'))
        if tier=='premium' and not facts.get('free_cl'): missing.append('измерение свободного/соответствующего хлора, не косвенного Rx')
        volume=facts.get('max_volume')
        if volume and Decimal(volume)<Decimal(snapshot['geometry']['volume']):
            return 'Известный предельный объём бассейна меньше расчётного.',[]
        if not volume: missing.append('допустимый объём бассейна при фактической нагрузке')
        missing.append('режим дозирования, реагенты, давление и блокировка по протоку')
        if mode=='semi_chlorine': return None,missing
    elif product['kind']=='control_panel':
        pumps=[r for b in variant['blocks'] for r in b['rows'] if r['kind']=='pump' and ((not b['excluded'] and not r['excluded']) or r['existing'])]
        if len(pumps)!=1 or pumps[0].get('quantity') is None or Decimal(pumps[0]['quantity'])!=1:
            return 'Для нескольких/неопределённых насосов нужна подтверждённая схема управления.',[]
        pump=pumps[0]; pf=(pump.get('catalog') or {}).get('auxiliary') or {}
        for limit,load,label in (('max_kw','motor_kw','мощность двигателя'),('max_current','motor_current','номинальный ток двигателя')):
            if facts.get(limit) and pf.get(load) and Decimal(facts[limit])<Decimal(pf[load]):
                return 'Известный предел панели ниже нагрузки насоса: '+label+'.',[]
            if not facts.get(limit): missing.append('допустимый '+label+' панели')
            if not pf.get(load): missing.append(label+' насоса')
        if not facts.get('pump_count'): missing.append('число каналов управления насосами')
        pump_power=(pump.get('catalog') or {}).get('power_options',[])
        panel_power=product.get('power_options',[])
        if pump_power and panel_power and not set(pump_power)&set(panel_power):
            return 'Известное питание панели не соответствует выбранному насосу.',[]
        missing.append('схема защиты, коммутации и блокировок выбранного насоса')
    else:
        return 'Назначение оборудования для этой строки не подтверждено.',[]
    options=product.get('power_options',[])
    power=snapshot['inputs']['power']
    if power!='unknown' and options and power not in options:
        return 'Известное питание оборудования не соответствует объекту.',[]
    if power=='unknown': missing.append('питание объекта')
    if not options: missing.append('питание оборудования')
    return None,list(dict.fromkeys(missing))


def fill(snapshot,catalog,only_variant=None):
    if snapshot.get('auxiliary_version')!=1: return 0
    from .estimate_catalog import apply_product,refresh_auto_facts,select_price_tier
    filled=0
    for tier,variant in snapshot['variants'].items():
        if only_variant and tier!=only_variant: continue
        for block in variant['blocks']:
            if block['excluded']: continue
            for row in block['rows']:
                if row['kind'] not in TITLES or row['excluded'] or row['existing']: continue
                origin=row.get('catalog')
                editable=not row.get('selection_locked') and not row.get('unit_price_manual') and row['unit']=='компл.' and row.get('quantity') is not None and Decimal(row['quantity'])==1
                auto=origin and origin.get('selection')=='auto' and row['price']==origin['price']
                placeholder=not origin and not row.get('model') and row['price'] is None and row['name'] in (TITLES[row['kind']],treatment_title(tier))
                if not editable or not (auto or placeholder):
                    if origin and origin['id'] in catalog['products']:
                        issue,_=assess(catalog['products'][origin['id']],snapshot,variant,tier)
                        row['catalog_review_note']='Текущая выгрузка: '+issue if issue else ''
                    continue
                choices=[]
                for product in catalog['products'].values():
                    if product['kind']!=row['kind']: continue
                    blocked,missing=assess(product,snapshot,variant,tier)
                    if not blocked:
                        desired={'economy':'semi_chlorine','standard':'rx_ph','premium':'cl_ph'}[tier]
                        mode_rank=int(row['kind']=='treatment' and (product.get('auxiliary') or {}).get('mode')!=desired)
                        choices.append((product,(mode_rank,len(missing))))
                if choices:
                    best=min(rank for _,rank in choices)
                    product=select_price_tier([p for p,rank in choices if rank==best],tier)
                    if origin and origin['id']==product['id']: refresh_auto_facts(row,product,catalog)
                    else: apply_product(row,product,catalog,'auto');filled+=1
                elif auto:
                    current=catalog['products'].get(origin['id']) or dict(origin,kind=row['kind'])
                    issue,_=assess(current,snapshot,variant,tier)
                    if issue:
                        row.update(catalog=None,price=None,model='',brand='',name=treatment_title(tier) if row['kind']=='treatment' else TITLES[row['kind']],selection_note='Проверить: прежний автоматический кандидат отклонён. '+issue)
    return filled


def annotate(snapshot,variant,tier):
    if snapshot.get('auxiliary_version')!=1: return
    for block in variant['blocks']:
        for row in block['rows']:
            if row['kind'] not in TITLES or block['excluded'] or row['excluded'] or not row.get('catalog'): continue
            origin=row['catalog'];product=dict(origin,kind=row['kind'],name=row['name'])
            blocked,missing=assess(product,snapshot,variant,tier)
            reason=row.get('catalog_review_note') or blocked or 'Не подтверждены: '+', '.join(missing)+'.'
            row['selection_note']=('Проверить: '+reason+' Источник: '+origin['url']+'; дата цены '+origin['checked_at']+'. Правило предложения: назначение, известные ограничения, полнота данных, цена в сегменте. Совместимость требует проверки.')[:1000]
            row['selection_status']='check_auxiliary'
            row['compatibility']={'message':row['selection_note'],'can_auto':False,'kind':row['kind']}
            variant['warnings'].append(row['name']+': '+row['selection_note'])


def search_reason(product,inputs,required_flow,tier):
    """Search has pool inputs, not a selected pump; state the missing load."""
    from .pool_estimates import geometry
    snapshot={'inputs':inputs,'geometry':geometry(inputs)}
    variant={'blocks':[{'excluded':False,'rows':[{'kind':'pump','quantity':'1',
        'excluded':False,'existing':False,'catalog':None}]}]}
    blocked,missing=assess(product,snapshot,variant,tier)
    return blocked or 'Проверить: '+', '.join(missing)+'.'
