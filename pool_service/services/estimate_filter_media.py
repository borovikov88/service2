"""Filter loading from explicit catalogue facts, never filter size or an assumed glass ratio."""
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
import re
from .estimate_catalog_params import text

QUARTZ_URL='https://shop.aqualine22.ru/basseini/aksessuary/prochie-aksessuary/kvarc-pesok.html'
GLASS_URL='https://shop.aqualine22.ru/pesok-steklyannyj-aquaviva-fraktsiya-05-15-mm.html'
TITLE='Фильтрующая загрузка: подобрать песок'
OFF='Связанный фильтр исключён или отсутствует'
NUM=r'(\d+(?:[.,]\d+)?)'
RANGE=NUM+r'\s*[-–—]\s*'+NUM+r'\s*мм'


def positive(value):
    try:
        v=Decimal(str(value).replace(',','.'))
        return format(v.normalize(),'f') if v.is_finite() and 0<v<=1000000 else None
    except Exception:
        return None


def classify(specs,name,description,category,offer,url):
    title,desc=text(name),text(description)
    media=bool(re.search(r'\b(?:песок\s+(?:кварцев\w*|стеклян\w*)|(?:кварцев\w*|стеклян\w*)\s+песок)\b',title))
    if url in {QUARTZ_URL,GLASS_URL}: media=True
    if specs['kind']!='filter' and not media:return
    fields={k:set() for k in ('quartz_kg','glass_kg','bag_kg','fraction_min','fraction_max')}
    conflict=False
    def add(key,value):
        nonlocal conflict
        v=positive(value)
        if v is None:conflict=True
        else:fields[key].add(v)
    labels={
        'quartz_kg':{'загрузка песка','загрузка песка, кг','загрузка кварцевого песка','загрузка кварцевого песка, кг','масса песка, кг','вес песка, кг','требуемая масса песка, кг'},
        'glass_kg':{'загрузка стеклянного песка','загрузка стеклянного песка, кг','масса стеклянной загрузки, кг'},
        'bag_kg':{'фасовка','масса упаковки','масса упаковки, кг','вес мешка','вес мешка, кг'},
    }
    unit_claims=set()
    for param in offer.findall('param'):
        label,value=text(param.get('name')),text(param.text)
        for key,aliases in labels.items():
            if label in aliases:
                m=re.fullmatch(NUM+r'\s*(?:кг)?',value)
                if m and ('кг' in label or 'кг' in value):add(key,m[1])
                else:conflict=True
        if label in {'фракция','фракция песка','рекомендуемая фракция песка'}:
            m=re.fullmatch(RANGE,value)
            if m:add('fraction_min',m[1]);add('fraction_max',m[2])
            else:conflict=True
        if media and label in {'единица цены','единица продажи','единица измерения'}:
            unit_claims.add('kg' if value in {'кг','kg'} else 'bag' if value in {'мешок','меш.','упаковка','упак.','шт.','шт'} else 'unknown')
    if specs['kind']=='filter':
        for source in (title,desc):
            for pattern,key in [
                (r'(?:загрузка|масса|вес)\s+(?:кварцевого\s+)?песка\s*[:=—-]?\s*'+NUM+r'\s*кг','quartz_kg'),
                (r'(?:загрузка|масса|вес)\s+стеклянного\s+песка\s*[:=—-]?\s*'+NUM+r'\s*кг','glass_kg')]:
                for m in re.finditer(pattern,source):add(key,m[1])
            for m in re.finditer(r'фракция(?:\s+песка)?\s*[:=—-]?\s*'+RANGE,source):
                add('fraction_min',m[1]);add('fraction_max',m[2])
    else:
        # A title weight or explicitly labelled package is sale mass; generic
        # shipping weight and a filter's weight are not package evidence.
        for m in re.finditer(NUM+r'\s*кг\b',title):add('bag_kg',m[1])
        for m in re.finditer(r'(?:фасовка|масса упаковки|вес мешка)\s*[:=—-]?\s*'+NUM+r'\s*кг',desc):add('bag_kg',m[1])
        for m in re.finditer(RANGE,title):add('fraction_min',m[1]);add('fraction_max',m[2])
    facts={key:next(iter(vals)) if len(vals)==1 else None for key,vals in fields.items()}
    conflict |= any(len(vals)>1 for vals in fields.values())
    if facts['fraction_min'] and facts['fraction_max'] and Decimal(facts['fraction_min'])>=Decimal(facts['fraction_max']):conflict=True
    if not media:
        specs['filter_loading']={k:facts[k] for k in ('quartz_kg','glass_kg','fraction_min','fraction_max')}
        specs['filter_loading']['conflict']=bool(conflict)
        denied=[]
        for material,word in [('quartz','кварцев'),('glass','стеклян')]:
            for clause in re.split(r'[;.!?]',title+'; '+desc):
                if re.search(r'(?:не\s+(?:использовать|применять)|запрещен\w*|не\s+подходит)\s+(?:песок\s+)?'+word+r'\w*(?:\s+песок)?',clause):denied.append(material)
                if re.search(word+r'\w*\s+песок\s+(?:не\s+подходит|запрещен\w*)',clause):denied.append(material)
        specs['filter_loading']['denied']=sorted(set(denied))
        return
    materials=set()
    if re.search(r'кварцев',title) or url==QUARTZ_URL:materials.add('quartz')
    if re.search(r'стеклян',title) or url==GLASS_URL:materials.add('glass')
    purpose='pool' if re.search(r'бассейн|песочн\w* фильтр|для фильтр',title+' '+desc+' '+text(category)) else 'unknown'
    for clause in re.split(r'[;.!?]',title+'; '+desc):
        if re.search(r'не\s+(?:(?:подходит|предназначен\w*)\s+)?(?:для|под)\s+(?:бассейн|фильтр)|(?:для бассейн\w*|для фильтр\w*)\s+не подходит',clause):purpose='denied'
        elif purpose!='denied':
            for match in re.finditer(r'для\s+(?:пескостру\w*|строительств\w*|раствор\w*)',clause):
                if re.search(r'не\s+(?:(?:подходит|предназначен\w*)\s+)?$',clause[:match.start()]):continue
                if re.match(r'\s+не\s+(?:подходит|предназначен\w*)',clause[match.end():]):continue
                purpose='other'
    specs['kind']='filter_media'
    specs['filter_media']={k:facts[k] for k in ('bag_kg','fraction_min','fraction_max')}
    specs['filter_media'].update(material=next(iter(materials)) if len(materials)==1 else '',purpose=purpose,
        price_unit=next(iter(unit_claims)) if len(unit_claims)==1 else 'bag' if not unit_claims and facts['bag_kg'] else 'unknown',
        conflict=bool(conflict or len(materials)!=1 or len(unit_claims)>1))


def normalize(raw,loading=False):
    from .pool_estimates import EstimateInputError
    if raw is None or raw=={}:return {}
    if not isinstance(raw,dict):raise EstimateInputError('Некорректные характеристики загрузки фильтра.')
    keys=('quartz_kg','glass_kg','fraction_min','fraction_max') if loading else ('bag_kg','fraction_min','fraction_max')
    out={}
    for key in keys:
        value=raw.get(key)
        if value is not None and positive(value) is None:raise EstimateInputError('Некорректная масса/фракция загрузки фильтра.')
        out[key]=positive(value) if value is not None else None
    if not isinstance(raw.get('conflict',False),bool):raise EstimateInputError('Некорректный конфликт загрузки фильтра.')
    out['conflict']=raw.get('conflict',False)
    if loading:
        denied=raw.get('denied',[])
        if not isinstance(denied,list) or len(denied)>2 or any(v not in {'quartz','glass'} for v in denied):raise EstimateInputError('Некорректный запрет загрузки фильтра.')
        out['denied']=list(dict.fromkeys(denied))
    if not loading:
        for key,allowed in [('material',{'quartz','glass',''}),('purpose',{'pool','unknown','other','denied'}),('price_unit',{'bag','kg','unknown'})]:
            if raw.get(key) not in allowed:raise EstimateInputError('Некорректные характеристики песка.')
            out[key]=raw[key]
    return out


def allrows(variant):return [r for b in variant['blocks'] for r in b['rows']]


def initialize(snapshot):
    snapshot['filter_media_version']=1
    sync(snapshot)


def protected(row):
    origin=row.get('catalog')
    return (row.get('existing') or row.get('selection_locked') or row.get('unit_price_manual')
            or not row.get('auto_quantity') or origin and (origin.get('selection')!='auto' or row['price']!=origin['price'])
            or not origin and (row.get('price') is not None or row.get('model')))


def assess(product,parent=None):
    f=product.get('filter_media') or {}
    if not product.get('available'):return 'Доступность товара к заказу не подтверждена.'
    if product.get('specification_conflict') or f.get('conflict'):return 'Противоречивые характеристики фильтрующей загрузки.'
    if f.get('material') not in {'quartz','glass'}:return 'Материал загрузки не определён.'
    if f.get('purpose') in {'other','denied'}:return 'Указано иное назначение или запрет применения в фильтре бассейна.'
    load=(parent or {}).get('filter_loading') or ((parent or {}).get('catalog') or {}).get('filter_loading',{})
    if load.get('conflict'):return 'Противоречивые данные загрузки выбранного фильтра.'
    if f.get('material') in load.get('denied',[]):return 'Паспорт выбранного фильтра запрещает этот материал загрузки.'
    if all(load.get(k) and f.get(k) for k in ('fraction_min','fraction_max')):
        if Decimal(f['fraction_min'])<Decimal(load['fraction_min']) or Decimal(f['fraction_max'])>Decimal(load['fraction_max']):
            return 'Фракция песка выходит за указанный диапазон выбранного фильтра.'
    return None


def quantity(row,parent):
    f=(row.get('catalog') or {}).get('filter_media') or {}
    load=(parent.get('catalog') or {}).get('filter_loading') or {}
    if f.get('material') in load.get('denied',[]):return None,'Паспорт выбранного фильтра запрещает этот материал загрузки.'
    if load.get('conflict') or f.get('conflict'):return None,'Противоречивые масса/фракция: количество не рассчитано.'
    material=f.get('material')
    mass=load.get('quartz_kg' if material=='quartz' else 'glass_kg') if material in {'quartz','glass'} else None
    if not mass:return None,('Не указана масса '+('стеклянной' if material=='glass' else 'кварцевой')+' загрузки выбранного фильтра. Коэффициент замены кварца стеклом не предполагается.')
    if parent['quantity'] is None:return None,'Не задано количество фильтров.'
    count=Decimal(parent['quantity'])
    if parent.get('unit') not in {'шт.','шт','компл.','компл','комплект'} or count!=count.to_integral_value():
        return None,'Не подтверждено целое количество фильтров в штуках/комплектах; единица строки не является массой загрузки.'
    total=Decimal(mass)*count
    if f.get('price_unit')=='kg':return str(total.quantize(Decimal('.001'))),'Масса загрузки: '+str(total)+' кг; цена за кг.'
    if f.get('price_unit')!='bag' or not f.get('bag_kg'):return None,'Не подтверждены фасовка и единица цены песка.'
    packs=(total/Decimal(f['bag_kg'])).to_integral_value(rounding=ROUND_CEILING)
    return str(packs.quantize(Decimal('.001'))),'Нужно '+str(total)+' кг; мешок '+f['bag_kg']+' кг; округление вверх до целого мешка, без дополнительного запаса.'


def sync(snapshot):
    if not snapshot.get('filter_media_version'):return
    from .pool_estimates import make_row
    for tier,v in snapshot['variants'].items():
        rows=allrows(v);parents={r['id']:r for r in rows if r['kind']=='filter'}
        initialized=v.setdefault('filter_media_parents',[])
        target=next(b for b in v['blocks'] if b['id']=='equipment')
        for pid,parent in parents.items():
            linked=[r for r in rows if r['kind']=='filter_media' and r.get('filter_parent')==pid]
            if not linked and pid not in initialized:
                legacy=[r for r in rows if r['kind'] in {'other','filter_media'} and not r.get('filter_parent') and re.search(r'песок.*(?:кварцев|стеклян)|(?:кварцев|стеклян).*песок|фильтрующ\w* загрузк',r['name'],re.I)]
                if not legacy and len(target['rows'])<100:
                    rid=('filter_media_'+pid)[:64]
                    if any(r['id']==rid for r in rows):continue
                    new=make_row(rid,TITLE,unit='меш.',quantity=None,basis='filter_load',kind='filter_media')
                    new['filter_parent']=pid;target['rows'].append(new);rows.append(new)
                initialized.append(pid)
        hidden={r['id'] for b in v['blocks'] if b['excluded'] for r in b['rows']}
        for r in rows:
            if r['kind']!='filter_media':continue
            parent=parents.get(r.get('filter_parent'))
            inactive=not parent or parent['excluded'] or parent['id'] in hidden or parent['quantity']=='0.000'
            if inactive:
                if not protected(r) and not r['excluded']:r.update(excluded=True,reason=OFF)
            elif r['excluded'] and r['reason']==OFF:r.update(excluded=False,reason='')
            if parent and r.get('auto_quantity') and r.get('basis')=='filter_load' and not r['existing']:
                value,note=quantity(r,parent)
                f=(r.get('catalog') or {}).get('filter_media') or {}
                expected='кг' if f.get('price_unit')=='kg' else 'меш.'
                if r['unit']==expected:r['quantity']=value
                else:r['quantity']=None;note='Единица строки отличается от единицы цены выбранного песка.'
                r['selection_note']='Проверить: '+note


def refresh_facts(row,product):
    """Sale mass and sale unit belong to the frozen price, not live engineering facts."""
    origin=row['catalog'];old=origin.get('filter_media') or {}
    current=deepcopy(product.get('filter_media',{}))
    changed=any(old.get(k)!=current.get(k) for k in ('bag_kg','price_unit'))
    if changed:
        for key in ('bag_kg','price_unit'):current[key]=old.get(key,'unknown' if key=='price_unit' else None)
    origin['filter_media']=current
    origin['specification_conflict']=product.get('specification_conflict',False)
    return 'Фасовка или единица цены в каталоге изменились. Сохранены прежние фасовка и цена; перевыберите товар или явно обновите цены.' if changed else ''


def fill(snapshot,catalog,only_variant=None):
    if not snapshot.get('filter_media_version'):return 0
    from .estimate_catalog import apply_product,refresh_auto_facts,select_price_tier
    count=0
    for tier,v in snapshot['variants'].items():
        if only_variant and tier!=only_variant:continue
        rows=allrows(v);parents={r['id']:r for r in rows if r['kind']=='filter'}
        # Refresh technical loading facts on all selected filters, with their
        # selected product/price and snapshot history left intact.
        for parent in parents.values():
            current=catalog['products'].get((parent.get('catalog') or {}).get('id'))
            if current:parent['catalog']['filter_loading']=deepcopy(current.get('filter_loading',{}))
        for b in v['blocks']:
            if b['excluded']:continue
            for r in b['rows']:
                if r['kind']!='filter_media' or r['excluded']:continue
                parent=parents.get(r.get('filter_parent'))
                origin=r.get('catalog')
                if origin:
                    current=catalog['products'].get(origin['id'])
                    issue=assess(current,parent) if current else 'Выбранная загрузка отсутствует в текущей выгрузке.'
                    pack_note=refresh_facts(r,current) if current else ''
                    r['catalog_review_note']=('Текущая выгрузка: '+issue if issue else '')+' '+pack_note
                    r['catalog_review_note']=r['catalog_review_note'].strip()
                if protected(r):continue
                parent=parents.get(r.get('filter_parent'))
                if not parent:continue
                origin=r.get('catalog')
                material='glass' if tier=='premium' else 'quartz'
                choices=[p for p in catalog['products'].values() if p['kind']=='filter_media' and p.get('filter_media',{}).get('material')==material and not assess(p,parent)]
                # Known packing and fraction evidence precede price and preferred URL.
                def rank(p):
                    f=p['filter_media'];load=(parent.get('catalog') or {}).get('filter_loading',{})
                    return (f.get('price_unit')=='unknown',not f.get('bag_kg') and f.get('price_unit')!='kg',not all(f.get(k) and load.get(k) for k in ('fraction_min','fraction_max')),f.get('purpose')!='pool')
                if choices:
                    best=min(map(rank,choices));choices=[p for p in choices if rank(p)==best]
                    preferred=[p for p in choices if p['url']==(GLASS_URL if tier=='premium' else QUARTZ_URL)]
                    p=select_price_tier(preferred or choices,tier)
                    if origin and origin['id']==p['id']:refresh_auto_facts(r,p,catalog)
                    else:apply_product(r,p,catalog,'auto');count+=1
                elif origin:
                    p=catalog['products'].get(origin['id']) or origin
                    if assess(p,parent):r.update(name=TITLE,brand='',model='',catalog=None,price=None)
    sync(snapshot)
    return count


def selected(row,product):
    if product['kind']!='filter_media':return
    f=product.get('filter_media',{})
    if row.get('auto_quantity') and row.get('basis')=='filter_load':row['unit']='кг' if f.get('price_unit')=='kg' else 'меш.'


def annotate(snapshot,variant):
    if not snapshot.get('filter_media_version'):return
    rows=allrows(variant);parents={r['id']:r for r in rows if r['kind']=='filter'}
    for b in variant['blocks']:
        if b['excluded']:continue
        for r in b['rows']:
            if r['kind']!='filter_media' or r['excluded']:continue
            parent=parents.get(r.get('filter_parent'));origin=r.get('catalog')
            notes=[r['catalog_review_note']] if r.get('catalog_review_note') else []
            if not parent:notes.append('Не определён связанный фильтр; автоматическая привязка к нескольким фильтрам не предполагается.')
            if not origin:notes.append('Не выбран подходящий кварцевый или стеклянный песок; цена не включена.')
            if origin:
                issue=assess(origin,parent)
                if issue:notes.append(issue)
                f=origin.get('filter_media') or {};load=((parent or {}).get('catalog') or {}).get('filter_loading') or {}
                if f.get('purpose')!='pool':notes.append('Назначение для фильтра бассейна не подтверждено.')
                if not all(f.get(k) and load.get(k) for k in ('fraction_min','fraction_max')):notes.append('Сверить фракцию и послойную схему засыпки с паспортом фильтра.')
                _,explanation=quantity(r,parent) if parent else (None,'Связь с фильтром отсутствует.')
                notes.append(explanation)
                if not r.get('auto_quantity'):notes.append('Количество задано вручную и сохранено.')
            r['selection_note']=('Проверить: '+' '.join(notes))[:1000]
            if notes:
                r['compatibility']={'message':r['selection_note'],'can_auto':False,'kind':'filter_media'}
                variant['warnings'].append(r['selection_note'])
