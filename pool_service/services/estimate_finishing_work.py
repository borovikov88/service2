"""Owner-defined contact-layer labor and stable initial stage-two work order."""
from .estimate_technology import mode

ID='work_contact_layer'
OFF='Контактный слой не требуется без активной штукатурки'
ORDER=('primer','contact_layer','plaster','embedded_work','finish_film','finish_mosaic','finish_tile',
       'underlay_film','waterproofing_mosaic','equipment_work','finish_transport','finish_travel')


def rows(variant):return [r for b in variant['blocks'] for r in b['rows']]


def initialize(snapshot):
    from .pool_estimates import make_row, EstimateInputError
    snapshot['finishing_work_version']=1
    for tier,v in snapshot['variants'].items():
        allrows=rows(v)
        if tier=='standard':
            for r in allrows:
                if r['id']=='tech_plaster_contact' and r['name']=='Litokol CR30 — контактный слой (уточнить расход)' and r.get('auto_quantity') and not r.get('catalog') and r['price'] is None and not r.get('selection_locked'):
                    r['name']='IDROKOL X20 + цемент М400–М500 — контактное молочко (уточнить расход)'

        if not v.get('contact_work_initialized'):
            existing=[r for r in allrows if r.get('work_code')=='contact_layer' or r['id']==ID or r['kind']=='work' and 'контактн' in r['name'].casefold()]
            if not existing:
                b=next(b for b in v['blocks'] if b['id']=='finish')
                if len(b['rows'])>=100:raise EstimateInputError('Недостаточно места для работы контактного слоя.')
                r=make_row(ID,'Нанесение контактного слоя','м²',None,'contact_work','work')
                r.update(work_code='contact_layer',price='250',rate_source='Согласованная ставка 250 ₽/м²')
                b['rows'].append(r)
            v['contact_work_initialized']=True
        allrows=rows(v)
        from .estimate_layout import group_for
        stage=[r for b in v['blocks'] for r in b['rows'] if group_for(b,r)=='installation_work']
        order=v.setdefault('view_order',{}).get('installation_work')
        contact=next((r['id'] for r in stage if r.get('work_code')=='contact_layer'),None)
        if order is None:
            rank={code:i for i,code in enumerate(ORDER)}
            v['view_order']['installation_work']=[r['id'] for r in sorted(stage,key=lambda r:rank.get(r.get('work_code') or r['id'],len(rank)))]
        elif contact and contact not in order:
            ordered=list(order)
            primer=next((r['id'] for r in stage if r.get('work_code')=='primer'),None)
            plaster=next((r['id'] for r in stage if r.get('work_code')=='plaster'),None)
            index=ordered.index(primer)+1 if primer in ordered else ordered.index(plaster) if plaster in ordered else 0
            ordered.insert(index,contact);v['view_order']['installation_work']=ordered
    sync(snapshot)


def sync(snapshot):
    if not snapshot.get('finishing_work_version'):return
    for v in snapshot['variants'].values():
        allrows=rows(v);sources=[r for r in allrows if r.get('work_code')=='plaster']
        source=sources[0] if len(sources)==1 else None
        hidden={r['id'] for b in v['blocks'] if b['excluded'] for r in b['rows']}
        for row in allrows:
            if row.get('work_code')!='contact_layer' or row.get('basis')!='contact_work' or row.get('existing'):continue
            active=mode(snapshot['inputs'],'plaster') in {'walls','walls_floor'} and source is not None and not source['excluded'] and source['id'] not in hidden
            if not active and not row['excluded']:row.update(excluded=True,reason=OFF)
            elif active and row['excluded'] and row['reason']==OFF:row.update(excluded=False,reason='')
            if row['auto_quantity']:
                row['quantity']=source['quantity'] if active and source['unit']=='м²' and row['unit']=='м²' else None
            row['construction_quantity_note']='Площадь совпадает с работой «Штукатурка», без запаса материалов. Ручное количество сохраняется.'
            if source is None:row['selection_note']='Проверить: не определена единственная строка штукатурных работ.'
