"""Installation take-off uses actual chargeable fittings, not hydraulic defaults."""
from decimal import Decimal
from .estimate_embedded import ROLES
POINT_KINDS={*ROLES,'level_inlet','pool_light','light_niche','light_box'}


def initialize(snapshot):
    snapshot['embedded_installation_version']=1
    for v in snapshot['variants'].values():
        for b in v['blocks']:
            for r in b['rows']:
                if r['id']=='embedded_work' and not r.get('basis') and r['unit'] in {'шт.','компл.'} and r['quantity'] is not None and Decimal(r['quantity'])==1 and r['price'] is None and not r.get('selection_locked') and not r.get('unit_price_manual') and not r['existing'] and not r.get('catalog'):
                    r.update(unit='шт.',basis='embedded_work',auto_quantity=True)


def apply(snapshot):
    if snapshot.get('embedded_installation_version')!=1: return
    for v in snapshot['variants'].values():
        totals={k:Decimal(0) for k in POINT_KINDS}; unknown=False
        for b in v['blocks']:
            if b['excluded']:continue
            for r in b['rows']:
                if r['kind'] not in POINT_KINDS or r['excluded'] or r['existing']:continue
                if r['quantity'] is None or r['unit']!='шт.': unknown=True;continue
                totals[r['kind']]+=Decimal(r['quantity'])
        # Lamp and its niche form one installation point; boxes are separate.
        qty=sum(totals.values())-min(totals['pool_light'],totals['light_niche'])
        for b in v['blocks']:
            for r in b['rows']:
                if r.get('basis')=='embedded_work' and r['auto_quantity'] and not r['existing']:
                    r['quantity']=str(qty) if not unknown and r['unit']=='шт.' else None
                    r['construction_quantity_note']='Фактические закладные: прожектор с закладной — одна точка; распределительные коробки отдельно. Исключённые и уже установленные позиции не начисляются.'
