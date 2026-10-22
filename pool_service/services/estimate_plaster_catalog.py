"""Catalogue identity and explicit packaging for the approved Litokol main layer."""
from copy import deepcopy
from decimal import Decimal as D, ROUND_HALF_UP
import re
from .estimate_catalog_params import text
N=r'(\d+(?:[.,]\d+)?)'
SOURCE='https://www.litokol.ru/catalog/litokol-cr30/'


def classify(specs,name,brand,description,offer):
    title=text(name);maker=text(brand)
    if not re.search(r'litokol|литокол',title+' '+maker):return
    component='cr30' if re.search(r'(?<![a-zа-я0-9])(?:cr|ср)\s*-?\s*30(?![a-zа-я0-9])',title) else 'idrokol' if re.search(r'\b(?:idrokol|идрокол)\s*[xх]\s*-?\s*20(?!\d)',title) else ''
    if not component:return
    masses=set();units=set();invalid=False;unit_claims=set()
    for m in re.finditer(N+r'\s*(кг|kg|л|л\.|литр\w*)\b',title):
        masses.add(str(D(m[1].replace(',','.')).normalize()));units.add('kg' if m[2] in {'кг','kg'} else 'l')
    for p in offer.findall('param'):
        label,value=text(p.get('name')),text(p.text)
        if label in {'фасовка','масса упаковки','вес упаковки','объем упаковки','фасовка, кг'}:
            m=re.fullmatch(N+r'\s*(кг|kg|л|литр\w*)?',value)
            if m and (m[2] or 'кг' in label):
                masses.add(str(D(m[1].replace(',','.')).normalize()));units.add('kg' if m[2] in {'кг','kg',None} else 'l')
            else:invalid=True
        if label in {'единица цены','единица продажи','единица измерения'}:
            unit_claims.add('kg' if value in {'кг','kg'} else 'l' if value in {'л','l','литр'} else 'package' if value in {'мешок','меш.','шт','шт.','упаковка','упак.','канистра'} else 'unknown')
    if not masses and component=='cr30':masses.add('25');units.add('kg') # Manufacturer's CR30 bag.
    quantity=next(iter(masses)) if len(masses)==1 else None
    unit=next(iter(units)) if len(units)==1 else ''
    specs.update(kind='plaster_component',plaster_product={'component':component,'pack_quantity':quantity,'pack_unit':unit,
        'price_unit':next(iter(unit_claims)) if len(unit_claims)==1 else 'package' if not unit_claims and quantity else 'unknown',
        'conflict':bool(invalid or len(masses)>1 or len(units)>1 or len(unit_claims)>1 or quantity and D(quantity)<=0)})


def normalize(raw):
    from .pool_estimates import EstimateInputError, number
    if raw is None or raw=={}:return {}
    if not isinstance(raw,dict) or raw.get('component') not in {'cr30','idrokol'} or raw.get('pack_unit') not in {'kg','l',''} or raw.get('price_unit') not in {'kg','l','package','unknown'} or not isinstance(raw.get('conflict'),bool):
        raise EstimateInputError('Некорректная фасовка Litokol.')
    value=number(raw.get('pack_quantity'),'Фасовка Litokol',minimum='.001',maximum='100000',places=None,optional=True)
    return {**{key:raw[key] for key in ('component','pack_unit','price_unit','conflict')},'pack_quantity':str(value) if value is not None else None}


def factor(row,product):
    facts=product.get('plaster_product') or {}
    if facts.get('conflict') or product.get('specification_conflict'):return None
    if facts.get('component')=='cr30' and row.get('basis')=='plaster_mix_bags' and row['unit']=='меш.':
        if facts.get('price_unit')=='kg':return D(25)
        if facts.get('price_unit')=='package' and facts.get('pack_unit')=='kg' and D(facts.get('pack_quantity') or 0)==25:return D(1)
    if facts.get('component')=='idrokol' and row.get('basis')=='plaster_latex_kg' and row['unit']=='кг':
        if facts.get('price_unit')=='kg':return D(1)
        if facts.get('price_unit')=='package' and facts.get('pack_unit')=='kg' and facts.get('pack_quantity'):return D(1)/D(facts['pack_quantity'])
    return None


def conversion(row,product):
    if product['kind']!='plaster_component':return
    row['material_price_suspended']=False
    f=factor(row,product)
    if f is None:
        row['price']=None;row['selection_note']='Проверить: фасовка/единица цены не позволяет применить цену к расчётной единице. Литры IDROKOL не приравниваются к кг.'
        return
    price=product['price'];row['catalog']['material_source_price']=price
    row['price']=row['catalog']['price']=str((D(price)*f).quantize(D('.01'),rounding=ROUND_HALF_UP))
    row['unit_price_manual']=False


def active(row):
    origin=row.get('catalog') or {}
    return (row['kind']=='plaster_component' and origin.get('material_source_price') and row['price'] is not None
        and not row.get('unit_price_manual') and row['price']==origin['price'] and factor(row,origin) is not None)


def amount(row):
    origin=row['catalog'];facts=origin['plaster_product']
    value=D(row['quantity'])*D(origin['material_source_price'])
    # Divide after multiplying. A recurring factor such as 1/3 must not move
    # a half-cent boundary before the final currency rounding.
    if facts['component']=='idrokol' and facts['price_unit']=='package':value/=D(facts['pack_quantity'])
    elif facts['component']=='cr30' and facts['price_unit']=='kg':value*=25
    return value.quantize(D('.01'),rounding=ROUND_HALF_UP)


def note(row):
    origin=row.get('catalog') or {}
    if row['kind']!='plaster_component':return ''
    if row.get('material_price_suspended'):return 'Проверить: единица строки не соответствует цене и фасовке Litokol. Автоматическая цена приостановлена; кг и литры не приравниваются.'
    if active(row):return 'Цена из каталога пересчитана в единицу строки по подтверждённой фасовке; сумма без промежуточного округления. '+row.get('catalog_review_note','')
    return row.get('selection_note') or 'Проверить цену, фасовку и единицу Litokol. Ручная цена, если задана, сохранена.'


def fill(snapshot,catalog,only_variant=None):
    from .estimate_catalog import apply_product
    count=0
    for tier,v in snapshot['variants'].items():
        if tier!='standard' or only_variant and tier!=only_variant:continue
        for b in v['blocks']:
            if b['excluded']:continue
            for row in b['rows']:
                component={'plaster_mix_bags':'cr30','plaster_latex_kg':'idrokol'}.get(row.get('basis'))
                if not component or row['excluded'] or row['existing'] or row.get('selection_locked') or row.get('unit_price_manual'):continue
                if row.get('catalog') or row['price'] is not None:continue
                choices=[p for p in catalog['products'].values() if p['kind']=='plaster_component' and p['available'] and p.get('plaster_product',{}).get('component')==component and not p['plaster_product'].get('conflict') and not p.get('specification_conflict')]
                confirmed=[p for p in choices if factor(row,p) is not None]
                if not choices:
                    row['selection_note']='Проверить: Litokol '+component+' не найден среди подходящих товаров выгрузки; цена не подставлена.'
                    continue
                product=min(confirmed or choices,key=lambda p:(D(p['price'])*(factor(row,p) or D(1)),int(p['id'])))
                apply_product(row,product,catalog,'auto');count+=1
    return count


def suspend(snapshot):
    for variant in snapshot['variants'].values():
        for b in variant['blocks']:
            for r in b['rows']:
                origin=r.get('catalog') or {}
                if r['kind']!='plaster_component' or not origin.get('material_source_price'):continue
                if r.get('unit_price_manual'):
                    r['material_price_suspended']=False
                    continue
                if factor(r,origin) is None and (r.get('material_price_suspended') or r['price']==origin['price']):
                    r.update(price=None,material_price_suspended=True)
                elif factor(r,origin) is not None and r.get('material_price_suspended'):
                    r.update(price=origin['price'],material_price_suspended=False)
