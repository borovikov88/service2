"""A separate supporting layer only when a filter explicitly requires gravel."""
from copy import deepcopy
from decimal import Decimal
from hashlib import sha256

from . import estimate_filter_media as media

TITLE='Гравий для фильтра: подобрать мешки'
OFF='Связанный фильтр исключён или отсутствует'


def initialize(snapshot,only=None):
    snapshot['filter_gravel_version']=1
    for tier,variant in snapshot['variants'].items():
        if not only or tier==only:variant['filter_gravel_active']=True
    sync(snapshot,only)


def loading(parent):
    return ((parent or {}).get('catalog') or {}).get('filter_loading') or {}


def requirement(parent):
    load=loading(parent)
    mass=load.get('gravel_kg')
    return None if load.get('conflict') or mass is None else Decimal(mass)


def protected(row):
    return (media.protected(row) or row.get('unit')!='меш.' or row.get('basis')!='filter_load'
            or row.get('excluded') and row.get('reason')!=OFF
            or not row.get('catalog') and row.get('name')!=TITLE)


def token(parent):
    value=requirement(parent)
    return (parent.get('catalog') or {}).get('id','')+':'+('unknown' if value is None else 'yes' if value>0 else 'no')


def sync(snapshot,only=None):
    if snapshot.get('filter_gravel_version')!=1:return
    from .pool_estimates import make_row
    for tier,variant in snapshot['variants'].items():
        if only and tier!=only or not variant.get('filter_gravel_active'):continue
        rows=media.allrows(variant)
        parents={r['id']:r for r in rows if r['kind']=='filter'}
        hidden={r['id'] for b in variant['blocks'] if b['excluded'] for r in b['rows']}
        remembered=variant.setdefault('filter_gravel_parents',{})
        deleted=variant.setdefault('filter_gravel_deleted',{})
        for pid in list(remembered):
            if pid not in parents:remembered.pop(pid)
        for pid in list(deleted):
            if pid not in parents:deleted.pop(pid)
        target=next(b for b in variant['blocks'] if b['id']=='equipment')
        remove=set()
        for row in rows:
            if row['kind']!='filter_gravel' or not row.get('filter_parent'):continue
            parent=parents.get(row['filter_parent'])
            required=requirement(parent)
            if not parent or parent['existing'] or required is None or required<=0:
                if not protected(row):remove.add(row['id'])
                if not parent or parent['existing']:remembered.pop(row['filter_parent'],None)
        for pid,parent in parents.items():
            product_id=(parent.get('catalog') or {}).get('id','')
            if deleted.get(pid) not in {None,product_id}:deleted.pop(pid)
            had_linked=any(r['kind']=='filter_gravel' and r.get('filter_parent')==pid for r in rows)
            if not had_linked and remembered.get(pid)==product_id+':yes':deleted[pid]=product_id
            linked=[r for r in rows if r['kind']=='filter_gravel' and r.get('filter_parent')==pid and r['id'] not in remove]
            manual=[r for r in linked if protected(r)]
            automatic=[r for r in linked if not protected(r)]
            keep=None if manual else next(iter(automatic),None)
            remove.update(r['id'] for r in automatic if r is not keep)
            required=requirement(parent)
            inactive=parent['existing'] or parent['excluded'] or pid in hidden or Decimal(parent['quantity'] or '0')<=0
            unbound=any(r['kind']=='filter_gravel' and not r.get('filter_parent') for r in rows)
            decision=token(parent)
            if required is not None and required>0 and not inactive and not linked and not unbound and deleted.get(pid)!=product_id and remembered.get(pid)!=decision and len(target['rows'])<100:
                rid='filter_gravel_'+sha256(pid.encode()).hexdigest()[:20]
                if not any(r['id']==rid for r in rows):
                    keep=make_row(rid,TITLE,'меш.',None,'filter_load','filter_gravel')
                    keep['filter_parent']=pid;target['rows'].append(keep);rows.append(keep)
            if not inactive:remembered[pid]=decision
            if keep:
                if inactive and not keep['excluded']:keep.update(excluded=True,reason=OFF)
                elif not inactive and keep['excluded'] and keep['reason']==OFF:keep.update(excluded=False,reason='')
        if remove:
            for block in variant['blocks']:block['rows']=[r for r in block['rows'] if r['id'] not in remove]
        for row in media.allrows(variant):
            if row['kind']!='filter_gravel' or row.get('existing') or not row.get('auto_quantity') or row.get('basis')!='filter_load':continue
            parent=parents.get(row.get('filter_parent'))
            if parent:
                value,note=media.quantity(row,parent)
                if row['unit']=='меш.':row['quantity']=value
                else:row['quantity']=None;note='Единица строки не подтверждает цену за мешок гравия.'
                row['selection_note']='Проверить: '+note


def assess(product,parent=None):
    owner=media.owner_gravel(product)
    issue=media.assess(product,parent,role='gravel')
    if owner and issue and issue.startswith('Фракция загрузки'):issue=None
    if issue:return issue
    facts=product.get('filter_media') or {}
    if not owner and (product.get('kind')!='filter_gravel' or facts.get('material')!='gravel'):
        return 'Товар не подтверждён как гравий; крупный кварцевый песок не подменяет гравий автоматически.'
    if parent and parent.get('existing'):return 'Необходимость новой загрузки существующего фильтра требует ручного подтверждения.'
    if parent and (requirement(parent) is None or requirement(parent)<=0):return 'Не подтверждена положительная масса гравия выбранного фильтра.'
    return None


def fill(snapshot,catalog,only=None):
    from .estimate_catalog import apply_product,refresh_auto_facts,select_price_tier
    # Explicit refresh accepts technical loading facts, never new package/price facts.
    for tier,variant in snapshot['variants'].items():
        if only and tier!=only:continue
        for parent in media.allrows(variant):
            origin=parent.get('catalog')
            current=catalog['products'].get(origin['id']) if parent['kind']=='filter' and origin else None
            if current and current['kind']=='filter':origin['filter_loading']=deepcopy(current.get('filter_loading',{}))
    initialize(snapshot,only)
    count=0
    for tier,variant in snapshot['variants'].items():
        if only and tier!=only:continue
        parents={r['id']:r for r in media.allrows(variant) if r['kind']=='filter'}
        for block in variant['blocks']:
            if block['excluded']:continue
            for row in block['rows']:
                if row['kind']!='filter_gravel' or row['excluded']:continue
                parent=parents.get(row.get('filter_parent'));origin=row.get('catalog')
                if origin:
                    current=catalog['products'].get(origin['id'])
                    issue=assess(current,parent) if current else 'Выбранный гравий отсутствует в текущей выгрузке.'
                    packing=media.refresh_facts(row,current) if current else ''
                    row['catalog_review_note']=' '.join(x for x in [('Текущая выгрузка: '+issue) if issue else '',packing] if x)
                    if packing and not issue:continue
                if protected(row) or not parent:continue
                choices=[p for p in catalog['products'].values() if (p['kind']=='filter_gravel' or media.owner_gravel(p)) and not assess(p,parent)
                         and p['filter_media'].get('price_unit')=='bag' and p['filter_media'].get('bag_kg')]
                known=[p for p in choices if all(p['filter_media'].get(k) and loading(parent).get('gravel_'+k) for k in ('fraction_min','fraction_max'))]
                chosen=select_price_tier(known or choices,tier)
                if chosen:
                    if origin and origin['id']==chosen['id']:refresh_auto_facts(row,chosen,catalog)
                    else:apply_product(row,chosen,catalog,'auto');count+=1
                elif origin:
                    row.update(name=TITLE,model='',brand='',price=None,catalog=None,catalog_review_note='')
    sync(snapshot,only)
    return count


def annotate(snapshot,variant):
    if snapshot.get('filter_gravel_version')!=1 or not variant.get('filter_gravel_active'):return
    rows=media.allrows(variant)
    hidden={r['id'] for b in variant['blocks'] if b['excluded'] for r in b['rows']}
    parents={r['id']:r for r in rows if r['kind']=='filter'}
    for parent in parents.values():
        if parent['excluded'] or parent['id'] in hidden or parent['existing'] or not parent.get('catalog'):continue
        required=requirement(parent)
        if required is None:
            variant['warnings'].append(parent['name']+': необходимость и масса отдельного гравия не подтверждены карточкой; отсутствующее поле не означает ноль.')
        elif required>0 and not any(r['kind']=='filter_gravel' and r.get('filter_parent')==parent['id'] and not r['excluded'] and r['id'] not in hidden for r in rows):
            variant['warnings'].append(parent['name']+': требуется '+str(required)+' кг гравия на фильтр; отдельная строка отсутствует или исключена, проверьте ручные позиции.')
    for row in rows:
        if row['kind']!='filter_gravel' or row['excluded'] or row['id'] in hidden:continue
        parent=parents.get(row.get('filter_parent'));origin=row.get('catalog')
        notes=[row['catalog_review_note']] if row.get('catalog_review_note') else []
        required=requirement(parent)
        if not parent:notes.append('Не определён связанный фильтр; необходимость гравия требует проверки.')
        elif parent['existing']:notes.append('Фильтр уже существует; необходимость новой покупки гравия подтвердить вручную.')
        elif required is None:notes.append('Не подтверждена масса гравия; ручная строка сохранена.')
        elif required<=0:notes.append('Карточка фильтра не требует гравия; ручная строка сохранена, проверьте дублирование.')
        if parent and required is not None and required>0:
            notes.append('На один фильтр нужно '+format(required,'f')+' кг гравия; песок рассчитан отдельно.')
        if not origin:notes.append('Не найден подтверждённый мешковый гравий: модель, масса мешка и цена требуют проверки; мешки не рассчитаны из фасовки песка.')
        else:
            issue=assess({**origin,'kind':'filter_gravel'},parent)
            if issue:notes.append(issue)
            if media.owner_gravel(origin):
                load=loading(parent);facts=origin['filter_media']
                if (media.assess(origin,parent,role='gravel') or '').startswith('Фракция загрузки'):
                    notes.append('Фракция гравия по карточке фильтра '+load['gravel_fraction_min']+'–'+load['gravel_fraction_max']+' мм; выбранный артикул '+facts['fraction_min']+'–'+facts['fraction_max']+' мм. Применено отдельное решение владельца, требуется проверка по паспорту.')
                notes.append(media.OWNER_GRAVEL_NOTE)
            if (origin.get('filter_media') or {}).get('packing_source'):notes.append(origin['filter_media']['packing_source'])
            _,explanation=media.quantity(row,parent) if parent else (None,'Связь с фильтром отсутствует.')
            notes.append(explanation)
        if not row.get('auto_quantity'):notes.append('Количество задано вручную и сохранено.')
        row['selection_note']=('Проверить: '+' '.join(notes))[:1000]
        row['compatibility']={'message':row['selection_note'],'can_auto':False,'kind':'filter_gravel'}
        variant['warnings'].append(row['selection_note'])
