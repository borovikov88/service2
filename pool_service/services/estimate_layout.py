"""Two customer stages over stable source rows; saved calculations are not relocated."""
from decimal import Decimal
import re

GROUPS = (
    ('stage1', 'Этап 1. Строительство чаши', (('bowl_work','Работы'),('bowl_materials','Материалы для чаши бассейна'))),
    ('stage2', 'Этап 2. Монтаж оборудования и отделка', (('embedded','Закладные элементы'),('equipment','Оборудование'),('materials','Материалы'),('installation_work','Работы'))),
)
IDS={key for _,_,groups in GROUPS for key,_ in groups}


def group_for(block, row):
    from .estimate_works import is_work
    if is_work(row):
        return 'bowl_work' if block['id']=='construction' and not re.search(r'transport|travel|handling|delivery',row.get('work_code','')) else 'installation_work'
    if block['id'] in {'construction','construction_materials'}: return 'bowl_materials'
    if row['kind'] in {'light_transformer','light_controller'}: return 'equipment'
    if row['id']=='piping': return 'materials'
    return block['id'] if block['id'] in {'embedded','equipment'} else 'materials'


def normalize_order(raw, row_ids):
    from .pool_estimates import EstimateInputError
    if not isinstance(raw,dict) or set(raw)-IDS: raise EstimateInputError('Некорректный порядок таблиц.')
    result={}
    for group, ids in raw.items():
        if not isinstance(ids,list) or len(ids)>700 or any(not isinstance(x,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',x) for x in ids) or len(set(ids))!=len(ids):
            raise EstimateInputError('Некорректный порядок строк таблицы.')
        result[group]=[rid for rid in ids if rid in row_ids]
    return result


def sections(variant, client=False):
    grouped={key:[] for key in IDS}
    for block in variant['blocks']:
        for row in block['rows']:
            grouped[group_for(block,row)].append((block,row))
    result=[]
    for stage,title,groups in GROUPS:
        tables=[]
        for key,label in groups:
            order=variant.get('view_order',{}).get(key,[])
            rank={rid:i for i,rid in enumerate(order)}
            entries=sorted(grouped[key],key=lambda pair:(pair[1]['id'] in {'pvc_embedded','piping'},rank.get(pair[1]['id'],len(rank))))
            if client: entries=[(b,r) for b,r in entries if not b['excluded'] and not r['excluded'] and not r['existing']]
            rows=[{**r,'source_block':b['id'],'source_excluded':b['excluded']} for b,r in entries]
            total=sum((Decimal(r['amount']) for b,r in entries if not b['excluded'] and not r['excluded'] and r.get('amount') is not None),Decimal(0))
            tables.append({'id':key,'title':label,'rows':rows,'subtotal':str(total)})
        result.append({'id':stage,'title':title,'blocks':tables})
    return result
