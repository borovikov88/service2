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
