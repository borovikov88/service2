"""Owner-specified 2 m liner angle: complete pieces, no invented retail price."""
from decimal import Decimal, ROUND_UP
URL='https://shop.aqualine22.ru/basseini/297-otdelochnye-materialy/307-lajner/krepegnyj-ugol-vneshnij-pvh-aquaviva-0050032-m.html'
ID='liner_corner_2m'
OFF='Уголок ПВХ применяется для плёнки'


def sync(snapshot,create=False):
    from .pool_estimates import make_row, EstimateInputError
    if snapshot.get('liner_corner_version')!=1:return
    perimeter=2*(Decimal(snapshot['inputs']['length'])+Decimal(snapshot['inputs']['width']))
    qty=(perimeter/2).quantize(Decimal(1),rounding=ROUND_UP)
    for v in snapshot['variants'].values():
        block=next(b for b in v['blocks'] if b['id']=='finish')
        row=next((r for b in v['blocks'] for r in b['rows'] if r['id']==ID),None)
        if create and not v.get('liner_corner_initialized'):
            if row is None:
                if len(block['rows'])>=100:raise EstimateInputError('Недостаточно места для уголка ПВХ.')
                row=make_row(ID,'Крепёжный уголок ПВХ Aquaviva 0050032 — отрезок 2 м','шт.',str(qty),'liner_corner')
                block['rows'].append(row)
            v['liner_corner_initialized']=True
        if not row:continue
        if row['auto_quantity'] and not row['existing']:row['quantity']=str(qty) if row['unit']=='шт.' else None
        enabled=snapshot['inputs']['finish']=='film'
        if not enabled and not row['excluded']:row.update(excluded=True,reason=OFF)
        elif enabled and row['excluded'] and row['reason']==OFF:row.update(excluded=False,reason='')
        row['construction_quantity_note']=f"Периметр {perimeter} м; {qty} отрезков по 2 м = {qty*2} м. Цена за один отрезок 2 м; проверьте единицу цены в карточке магазина. Без дополнительного запаса."


def initialize(snapshot):
    snapshot['liner_corner_version']=1
    sync(snapshot,create=True)


def classify(specs,name,brand,offer,url):
    import re
    sku=offer.findtext('vendorCode','')
    if url==URL or (brand.casefold()=='aquaviva' and (sku in {'0050032','0050032M'} or re.search(r'(?<!\d)0050032(?!\d)',name)) and re.search(r'угол',name,re.I)):
        specs['kind']='liner_corner'


def fill(snapshot,catalog,only_variant=None):
    from .estimate_catalog import apply_product
    count=0
    for tier,v in snapshot['variants'].items():
        if only_variant and tier!=only_variant:continue
        for b in v['blocks']:
            if b['excluded']:continue
            for row in b['rows']:
                if row['id']!=ID or row['excluded'] or row['existing'] or row.get('selection_locked') or row.get('unit_price_manual'):continue
                if row.get('catalog') or row['price'] is not None or row['model'] or row['unit']!='шт.':continue
                choices=[p for p in catalog['products'].values() if p['kind']=='liner_corner' and p['available'] and not p.get('specification_conflict')]
                exact=[p for p in choices if p['url']==URL]
                if not choices:
                    row['selection_note']='Проверить: указанный крепёжный уголок не найден среди доступных товаров выгрузки. Цена и изображение не выдумываются.'
                    continue
                product=min(exact or choices,key=lambda p:int(p['id']))
                apply_product(row,product,catalog,'auto');count+=1
    return count
