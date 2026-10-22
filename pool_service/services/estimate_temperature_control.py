"""Temperature regulation is a capability, not a synonym for a timer/display."""
import re
from copy import deepcopy
from decimal import Decimal

THERMOSTAT_URL = 'https://shop.aqualine22.ru/basseini/zapchasti/zapchasti-dlya-teploobmennikov-i-elektronagrevatelej/tsifrovoj-kontroller-termostat-aquaviva.html'
AVP_SOURCE = 'https://bas.ru/docs/av-avp_2647.pdf'
AVP_URL = 'https://shop.aqualine22.ru/basseini/274-oborudovanie-dlya-bassejnov/343-stantsii-kontrolya-i-upravleniya-bassejnom/panel-upravleniya-filtratsiej-i-nagrevom-aquaviva-avp-01.html'
PANEL = 'Температурой управляет выбранная панель'
OFF = 'Теплообменник отключён — термостат не требуется'
TITLE = 'Термостат теплообменника: подобрать модель'
STATES = {'yes', 'no', 'unknown'}


def normalize(raw):
    from .pool_estimates import string, boolean, EstimateInputError
    raw = {} if raw is None else raw
    if not isinstance(raw, dict): raise EstimateInputError('Некорректные данные температурного управления.')
    result = {k: string(raw.get(k, 'unknown'), 'Температурное управление', 20) for k in ('regulation', 'exchanger', 'sensor')}
    if any(v not in STATES for v in result.values()): raise EstimateInputError('Некорректное температурное управление.')
    result.update(conflict=boolean(raw.get('conflict', False)), source=string(raw.get('source', ''), 'Источник температурного управления', 500))
    return result


def classify(specs, name, brand='', description='', offer=None, url=''):
    title = name.casefold().replace('ё', 'е')
    desc = re.sub(r'<[^>]*>', ' ', description).casefold().replace('ё', 'е')
    preferred = url.rstrip('/') == THERMOSTAT_URL or bool(re.fullmatch(r'цифровой\s+контроллер[\s—–-]+термостат\s+aquaviva', title.strip()))
    if preferred or (re.match(r'^(?:цифровой\s+)?(?:контроллер[\s—–-]+термостат|термостат)\b', title) and re.search(r'бассейн|теплообменник', title+' '+desc)):
        specs['kind'] = 'thermostat'
    if specs.get('kind') not in {'control_panel', 'thermostat'}: return
    claims = {k:set() for k in ('regulation','exchanger','sensor')}
    avp = (specs['kind']=='control_panel' and (url.rstrip('/')==AVP_URL or
           re.search(r'(?<![\w-])avp[ -]01(?![\w-])',title) and 'aquaviva' in (title+' '+brand.casefold())))
    if avp:
        for key in claims: claims[key].add('yes')
    if preferred:
        claims['regulation'].add('yes'); claims['exchanger'].add('yes')
    expressions = {
        'regulation': r'(?:регулирован\w*|поддержан\w*|установк\w*|управлени\w*)\s+(?:заданн\w*\s+)?температур\w*',
        'exchanger': r'управлени\w*\s+(?:нагревом\s+)?теплообменник\w*',
        'sensor': r'датчик\w*\s+температур\w*',
    }
    for clause in re.split(r'[.;!?\n]+', title+'; '+desc):
        if re.search(r'не\s+регулиру\w*\s+температур|только\s+(?:отображени\w*|индикаци\w*)\s+температур',clause):claims['regulation'].add('no')
        if re.search(r'не\s+(?:управля\w*|поддержива\w*)\s+теплообменник',clause):claims['exchanger'].add('no')
        if re.search(r'не\s+(?:подходит|предназначен\w*)\s+для\s+теплообменник|для\s+теплообменник\w*\s+не\s+(?:подходит|предназначен\w*)',clause):claims['exchanger'].add('no')
        if re.search(r'не\s+(?:(?:подходит|предназначен\w*)\s+)?для\s+бассейн|для\s+бассейн\w*\s+не\s+подходит',clause):claims['exchanger'].add('no')
        if re.search(r'без\s+управлени\w*\s+нагрев|управлени\w*\s+нагрев\w*\s+(?:не\s+(?:поддержива\w*|предусмотр\w*)|отсутств\w*)',clause):claims['exchanger'].add('no')
        for key, pattern in expressions.items():
            for match in re.finditer(pattern,clause):
                before, after = clause[:match.start()],clause[match.end():]
                denied = bool(re.search(r'(?:без|не\s+поддерживает|не\s+включает|не\s+предусмотрено)\s*$',before)
                              or re.match(r'\s*(?:не\s+(?:поддержива\w*|предусмотр\w*|входит)|отсутств\w*|нет)\b',after))
                if denied: claims[key].add('no')
                elif key!='sensor' or re.match(r'\s+(?:в\s+комплекте|включен\w*|входит)',after): claims[key].add('yes')
    labels={'регулирование температуры':'regulation','управление теплообменником':'exchanger','датчик температуры в комплекте':'sensor'}
    if offer is not None:
        for param in offer.findall('param'):
            key=labels.get((param.get('name') or '').casefold().strip())
            value=(param.text or '').casefold().strip()
            if key and value in {'да','есть','true','1','нет','отсутствует','false','0'}:
                claims[key].add('yes' if value in {'да','есть','true','1'} else 'no')
    specs['temperature']={k:next(iter(v)) if len(v)==1 else 'unknown' for k,v in claims.items()}
    wrong=False
    if specs['kind']=='thermostat':
        text=title+' '+desc
        for match in re.finditer(r'(?:для|под)\s+(?:саун\w*|бан\w*|отопления\s+дома)',text):
            if re.search(r'не\s+(?:(?:подходит|предназначен\w*)\s+)?$',text[:match.start()]):continue
            if re.match(r'\s+не\s+(?:подходит|предназначен\w*)',text[match.end():]):continue
            wrong=True
    specs['temperature'].update(conflict=wrong or any(len(v)>1 for v in claims.values()),source=AVP_SOURCE if avp else THERMOSTAT_URL if preferred else 'Явные характеристики карточки товара')


def protected(row):
    origin=row.get('catalog')
    return (row.get('selection_locked') or row.get('unit_price_manual') or not row['auto_quantity']
            or origin and (origin.get('selection')!='auto' or row['price']!=origin['price'])
            or not origin and (row['price'] is not None or row['model']))


def active_panels(variant):
    return [r for b in variant['blocks'] if not b['excluded'] for r in b['rows'] if r['kind']=='control_panel' and not r['excluded'] and (r['quantity'] is None or Decimal(r['quantity'])>0)]


def panel_reason(snapshot, variant, tier, parents):
    from .estimate_auxiliary import assess
    panels=active_panels(variant)
    if len(panels)!=1:return None
    for row in panels:
        origin=row.get('catalog') or {}
        if row.get('catalog_review_note'):continue
        product=dict(origin,kind='control_panel',name=row['name'])
        if row['existing']:product['available']=True
        facts=origin.get('temperature')
        if not facts or (not facts.get('source') and not facts.get('conflict') and all(facts.get(k,'unknown')=='unknown' for k in ('regulation','exchanger','sensor'))):
            classify(product,row['name'],row['brand'],url=origin.get('url',''))
            facts=product.get('temperature',{})
        if facts.get('conflict') or facts.get('sensor')=='no':continue
        if facts.get('regulation')!='yes' or facts.get('exchanger')!='yes':continue
        if len(parents)!=1 or parents[0]['unit'] not in {'шт.','компл.'} or row['unit'] not in {'шт.','компл.'} or Decimal(parents[0]['quantity'] or 0)!=1 or Decimal(row['quantity'] or 0)!=1:
            continue  # Multiple units/circuits require an explicit assignment.
        issue,_=assess(product,snapshot,variant,tier)
        if not issue:return row
    return None


def sync(snapshot,create=False):
    if snapshot.get('temperature_control_version')!=1:return
    from .pool_estimates import make_row, row_heating_type, heating_types, exchanger_count, EstimateInputError
    for tier,variant in snapshot['variants'].items():
        equipment=next(b for b in variant['blocks'] if b['id']=='equipment')
        all_rows=[r for b in variant['blocks'] for r in b['rows']]
        parents={r['id']:r for r in all_rows if r['kind']=='heater' and row_heating_type(r,snapshot['inputs'])=='exchanger'}
        active=[r for b in variant['blocks'] if not b['excluded'] for r in b['rows'] if r['id'] in parents and not r['excluded'] and 'exchanger' in heating_types(snapshot['inputs'])]
        initialized=variant.setdefault('thermostat_parents',[])
        legacy=[r for r in all_rows if r['kind']=='other' and re.match(r'^(?:цифровой\s+)?(?:контроллер[\s—–-]+термостат|термостат)\b',r['name'],re.I)]
        if create and 'exchanger' in heating_types(snapshot['inputs']):
            for pid,parent in parents.items():
                if pid in initialized:continue
                if not any(r['kind']=='thermostat' and r.get('heating_parent')==pid for r in all_rows):
                    if legacy:
                        for old in legacy:old['selection_note']='Проверить: уже есть ручная строка термостата. Подтвердите её назначение и связь с теплообменником; новая строка не добавлена во избежание двойного начисления.'
                        initialized.append(pid);continue
                    unlinked=[r for r in all_rows if r['kind']=='thermostat' and not r.get('heating_parent')]
                    if unlinked:
                        if len(parents)==len(unlinked)==1 and not unlinked[0].get('heating_unbound'):
                            unlinked[0]['heating_parent']=pid;initialized.append(pid)
                        continue
                    if len(equipment['rows'])>=100:raise EstimateInputError('Недостаточно места для термостата.')
                    ids={r['id'] for r in all_rows}
                    rid=next(f'thermostat_{i}' for i in range(100) if f'thermostat_{i}' not in ids)
                    row=make_row(rid,TITLE,'шт.',exchanger_count(parent),'exchanger_quantity','thermostat')
                    row['heating_parent']=pid;equipment['rows'].append(row);all_rows.append(row)
                initialized.append(pid)
        panel=panel_reason(snapshot,variant,tier,active)
        for row in all_rows:
            if row['kind']!='thermostat' or row['existing']:continue
            parent=parents.get(row.get('heating_parent'))
            if row['auto_quantity'] and row['basis']=='exchanger_quantity':
                row['quantity']=exchanger_count(parent) if parent and row['unit']=='шт.' else None
            enabled=parent and any(p['id']==parent['id'] for p in active)
            reason=OFF if not enabled and row.get('heating_parent') else PANEL if panel and not protected(row) else ''
            if reason and (not row['excluded'] or row['reason'] in {OFF,PANEL}):row.update(excluded=True,reason=reason)
            elif not reason and row['excluded'] and row['reason'] in {OFF,PANEL}:row.update(excluded=False,reason='')
            if not row['excluded']:
                facts=(row.get('catalog') or {}).get('temperature',{})
                missing=[label for key,label in (('regulation','регулирование температуры'),('exchanger','управление теплообменником'),('sensor','комплектность датчика температуры')) if facts.get(key)!='yes'] if row.get('catalog') else []
                row['selection_note']=('Проверить: ' + ('панель уже регулирует температуру; сохранён ручной термостат. ' if panel else '')
                    + (row.get('catalog_review_note','')+' ' if row.get('catalog_review_note') else '')
                    + ('Не подтверждены: '+', '.join(missing)+'. ' if missing else '')
                    + 'Схема коммутации, нагрузка контактов, датчик и связь с теплообменником требуют проверки. '
                    + ('Укажите связь с теплообменником. ' if not parent else '')
                    + ('Для нескольких теплообменников нужна явная схема управления. ' if len(active)>1 else '')
                    + ('Для нескольких панелей нужна явная связь с теплообменником. ' if len(active_panels(variant))>1 else '')
                    + ('Автоматическая модель не найдена; эконом: Aquaviva, '+THERMOSTAT_URL if not row.get('catalog') else ''))[:1000]


def initialize(snapshot):
    snapshot['temperature_control_version']=1
    sync(snapshot,create=True)


def issue(product):
    facts=product.get('temperature',{})
    if product.get('kind') not in {None,'thermostat'}:return 'Категория товара больше не соответствует термостату.'
    if facts.get('conflict') or product.get('specification_conflict'):return 'Противоречивые характеристики температурного управления.'
    if facts.get('exchanger')=='no':return 'Карточка прямо исключает применение с теплообменником.'
    if facts.get('regulation')=='no':return 'Регулирование температуры прямо исключено карточкой товара.'
    return ''


def annotate(variant):
    for row in active_panels(variant):
        facts=(row.get('catalog') or {}).get('temperature',{})
        if facts.get('regulation')=='yes' and facts.get('exchanger')=='yes' and facts.get('sensor')=='unknown':
            note='Проверить комплектность и подключение датчика температуры панели.'
            row['selection_note']=(note+' '+row.get('selection_note',''))[:1000]
            if row.get('compatibility'):row['compatibility']['message']=row['selection_note']
            variant['warnings'].append(row['name']+': '+note)


def fill(snapshot,catalog,only=None):
    from .estimate_catalog import apply_product,refresh_auto_facts,select_price_tier
    # Explicit selection can refresh capability evidence without changing a
    # manually chosen panel or its frozen catalogue price.
    for tier,variant in snapshot['variants'].items():
        if only and only!=tier:continue
        for b in variant['blocks']:
            for row in b['rows']:
                origin=row.get('catalog')
                live=catalog['products'].get(origin['id']) if origin else None
                if row['kind']=='control_panel' and live:
                    origin['temperature']=deepcopy(live.get('temperature',normalize({})))
                    if live['kind']!='control_panel':origin['temperature']={**normalize({}),'source':'Назначение панели в текущей карточке не подтверждено'}
    sync(snapshot,create=True);count=0
    for tier,variant in snapshot['variants'].items():
        if only and only!=tier:continue
        for block in variant['blocks']:
            if block['excluded']:continue
            for row in block['rows']:
                if row['kind']!='thermostat':continue
                origin=row.get('catalog')
                current=catalog['products'].get(origin['id']) if origin else None
                if current:row['catalog_review_note']=issue(current)
                if current and issue(current) and not row['existing'] and not protected(row):
                    row.update(catalog=None,price=None,model='',brand='',name=TITLE)
                if row['excluded'] or row['existing'] or protected(row) or row['unit']!='шт.':continue
                choices=[p for p in catalog['products'].values() if p['kind']=='thermostat' and p['available'] and not issue(p)]
                if not choices:
                    if current and issue(current):row.update(catalog=None,price=None,model='',brand='',name=TITLE)
                    continue
                if tier=='economy':
                    preferred=[p for p in choices if p['url'].rstrip('/')==THERMOSTAT_URL or p.get('temperature',{}).get('source')==THERMOSTAT_URL]
                    if preferred:choices=preferred
                best=min(sum(p.get('temperature',{}).get(k)!='yes' for k in ('regulation','exchanger')) for p in choices)
                choices=[p for p in choices if sum(p.get('temperature',{}).get(k)!='yes' for k in ('regulation','exchanger'))==best]
                product=select_price_tier(choices,tier)
                if row.get('catalog') and row['catalog']['id']==product['id']:refresh_auto_facts(row,product,catalog)
                else:apply_product(row,product,catalog,'auto');count+=1
    sync(snapshot)
    return count
