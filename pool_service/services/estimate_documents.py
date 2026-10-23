"""Customer documents from frozen quote rows. No catalogue fetch or new pricing."""
from copy import deepcopy
from collections import Counter
from decimal import Decimal
from datetime import date
from .estimate_layout import sections

TIERS = {'economy': 'Эконом', 'standard': 'Стандарт', 'premium': 'Премиум'}
TERMS = {
    'equipment_warranty': 'Гарантия на оборудование',
    'installation_warranty': 'Гарантия на работы',
    'delivery_terms': 'Сроки поставки и выполнения работ',
    'payment_terms': 'Порядок оплаты',
    'validity': 'Срок действия предложения',
    'included': 'Что входит в стоимость',
    'not_included': 'Что оплачивается отдельно / выполняет заказчик',
    'manager': 'Ответственный менеджер и контакты',
    'conditions': 'Дополнительные условия',
}
CLIENT = {'client_name': 200, 'client_address': 500, 'object_address': 500, 'proposal_number': 80, 'proposal_date': 10}
ROLES = (
    ('pump', 'Фильтрационный насос', 'Обеспечивает циркуляцию воды через систему фильтрации.'),
    ('filter', 'Фильтр', 'Удаляет механические загрязнения при работе с выбранной загрузкой.'),
    ('filter_media', 'Фильтрующая загрузка', 'Материал для механической очистки воды в фильтре.'),
    ('heater', 'Нагрев воды', 'Предлагаемый источник подогрева воды бассейна.'),
    ('treatment', 'Дозирование', 'Предлагаемая комплектация системы обработки воды.'),
    ('control_panel', 'Панель управления', 'Объединяет подтверждённые функции управления оборудованием.'),
    ('skimmer', 'Скиммеры', 'Забор воды с поверхности бассейна.'),
    ('inlet', 'Форсунки', 'Подача очищенной воды в бассейн.'),
    ('drain', 'Донный слив', 'Предлагаемый донный элемент бассейна.'),
    ('pool_light', 'Освещение', 'Предлагаемые светильники бассейна.'),
    ('light_transformer', 'Трансформатор освещения', 'Питание светильников по выбранной схеме.'),
    ('light_controller', 'Контроллер освещения', 'Управление выбранной системой освещения.'),
)


def normalize(raw):
    from .pool_estimates import EstimateInputError, string, boolean
    if raw is None: raw = {}
    if not isinstance(raw, dict): raise EstimateInputError('Некорректные параметры документов.')
    result = {k: string(raw.get(k, ''), k, limit) for k, limit in CLIENT.items()}
    result.update({k: string(raw.get(k, ''), label, 2000) for k, label in TERMS.items()})
    for field, allowed, default in [('variants', TIERS, ['economy', 'standard']), ('types', {'estimate', 'equipment'}, ['estimate', 'equipment'])]:
        values = raw.get(field, default)
        if not isinstance(values, list) or not values or len(values)>len(allowed) or any(not isinstance(v,str) or v not in allowed for v in values) or len(set(values))!=len(values):
            raise EstimateInputError('Выберите варианты и типы документов.')
        result[field] = [v for v in allowed if v in values] if field=='variants' else values[:]
    result['technical_reviewed'] = boolean(raw.get('technical_reviewed', False))
    if result['proposal_date']:
        try: date.fromisoformat(result['proposal_date'])
        except ValueError: raise EstimateInputError('Некорректная дата предложения.') from None
    return result


def defaults(organization):
    from pool_service.estimate_models import PoolEstimateDocumentSettings
    stored=PoolEstimateDocumentSettings.objects.filter(organization=organization).values_list('values',flat=True).first() or {}
    return {key:stored.get(key,'') for key in TERMS}


def can_manage_defaults(user, organization):
    from pool_service.models import OrganizationAccess
    return OrganizationAccess.objects.filter(user=user,organization=organization,role__in=['owner','admin']).exists()


def readiness(snapshot, options):
    errors=[]
    if not snapshot.get('visible_rounding'): errors.append('Пересчитайте параметры: сохранённая версия использует прежнее правило округления.')
    for field,label in [('client_name','Наименование клиента'),('object_address','Адрес объекта'),('equipment_warranty','Гарантия на оборудование'),('installation_warranty','Гарантия на работы')]:
        if not options.get(field,'').strip(): errors.append('Заполните поле «'+label+'».')
    if not options.get('technical_reviewed'): errors.append('Проверьте комплектацию и технические замечания перед выдачей предложения.')
    for key in options['variants']:
        variant=snapshot['variants'][key]
        active=[r for b in variant['blocks'] if not b['excluded'] for r in b['rows'] if not r['excluded'] and not r['existing']]
        if not active: errors.append(TIERS[key]+': нет позиций для печати.')
        for row in active:
            if row.get('price') is None or row.get('quantity') is None or row.get('amount') is None:
                errors.append(TIERS[key]+': уточните цену/количество или исключите «'+row['name']+'».')
    return errors


def title(row):
    name,model=row.get('name',''),row.get('model','')
    return name+' · '+model if model and model.casefold() not in name.casefold() else name


def characteristics(row):
    origin=row.get('catalog') or {}; specs=row.get('specs') or {}; result=[]
    for field,label,unit in [('flow','Производительность','м³/ч'),('head','Напор','м'),('filter_flow','Производительность фильтра','м³/ч')]:
        if specs.get(field): result.append(f'{label}: {specs[field]} {unit}')
    if origin.get('heating_power'): result.append('Мощность нагрева: '+origin['heating_power']+' кВт')
    material={'plastic':'Пластик','steel304':'Нержавеющая сталь AISI 304','steel316':'Нержавеющая сталь AISI 316'}.get(origin.get('material'))
    if material: result.append(material)
    if origin.get('power') and origin['power']!='unknown': result.append('Питание: '+origin['power']+' В')
    return result


def build(snapshot, options):
    from .estimate_catalog import safe_image_url
    variants=[]; comparison=[]; cards={}
    for key in options['variants']:
        variant=snapshot['variants'][key]
        variants.append({'key':key,'title':TIERS[key],'total':variant['total'],'sections':sections(variant,client=True)})
    from .estimate_works import is_work
    from .estimate_rounding import quantity as shown_quantity, price as shown_price
    descriptions={kind:(label,purpose) for kind,label,purpose in ROLES}
    groups={}
    for key in options['variants']:
        for block in snapshot['variants'][key]['blocks']:
            if block['excluded']:continue
            for row in block['rows']:
                if row['excluded'] or row['existing'] or is_work(row):continue
                group_key=('role',row['kind']) if row['kind'] in descriptions else ('row',row['id'])
                label,purpose=descriptions.get(row['kind'],(row['name'],''))
                group=groups.setdefault(group_key,{'label':label,'purpose':purpose,'rows':{tier:[] for tier in options['variants']}})
                origin=row.get('catalog') or {}
                facts=characteristics(row)
                item={'name':title(row),'quantity':row['quantity'],'unit':row['unit'],
                    'price':row['price'],'amount':row.get('amount'),'facts':facts,
                    'image':safe_image_url(origin.get('image','')),
                    'show_unit_price':row['quantity'] is not None and Decimal(row['quantity'])!=1}
                # Compare what the customer sees, including quantity and cost.
                # A matching model with a different price/quantity is a difference.
                signature=(origin.get('id') or title(row),title(row),row['unit'],
                    shown_quantity(row['quantity'],row['unit']),shown_price(row['price']),
                    Decimal(row['amount']) if row.get('amount') is not None else None,tuple(facts))
                group['rows'][key].append((signature,item))
    for group in groups.values():
        common=None
        for rows in group['rows'].values():
            counts=Counter(signature for signature,_ in rows)
            common=counts if common is None else common & counts
        cells=[]
        for key,rows in group['rows'].items():
            remaining=common.copy();items=[]
            for signature,item in rows:
                if remaining[signature]:
                    remaining[signature]-=1
                    identity=signature[:2]+(tuple(item['facts']),)
                    card=cards.setdefault(identity,{'name':item['name'],'purpose':group['purpose'],
                        'image':item['image'],'facts':item['facts'],'variants':[TIERS[t] for t in options['variants']]})
                    if not card['image'] and item['image']:card['image']=item['image']
                else:items.append(item)
            cells.append(items)
        if any(cells):comparison.append({'label':group['label'],'cells':cells})
    # No separate catalogue IDs/brand lines, selection notes or compatibility claims are rendered.
    terms=[{'title':label,'text':options.get(key,'')} for key,label in TERMS.items() if options.get(key,'').strip()]
    pool=snapshot['inputs'];geometry=snapshot.get('geometry',{})
    return {'variants':variants,'comparison':comparison,'cards':list(cards.values()),'terms':terms,'options':deepcopy(options),
        'pool':pool,'geometry':geometry,'finish':{'film':'Плёнка ПВХ','tile':'Плитка','mosaic':'Мозаика'}.get(pool.get('finish'),''),
        'proposal_date':date.fromisoformat(options['proposal_date']).strftime('%d.%m.%Y') if options.get('proposal_date') else '',
        'landscape':len(variants)==3}
