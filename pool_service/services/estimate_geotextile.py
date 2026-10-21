"""Owner-confirmed catalogue price per m²; no roll conversion or extra stock."""
import re
from html import unescape


def classify(specs, name, description='', category='', offer=None):
    title = name.casefold().replace('ё', 'е')
    if not re.match(r'^геотекстиль\b', title):
        return
    sources = [unescape(re.sub(r'<[^>]*>', ' ', value or '')).casefold().replace('ё', 'е')
               for value in (title, description, category)]
    pool = other = denied = False
    # Keep negation local to one purpose claim, not the whole description.
    purposes = r'бассейн\w*|(?:под|для)\s+(?:пленк\w*|лайнер\w*)|дорожн\w*|дорог\w*|геодренаж\w*|ландшафт\w*|тротуар\w*'
    negative = r'\bне\s+(?:подход\w*|предназнач\w*|пригод\w*|рекоменд\w*|использ\w*|примен\w*)'
    for source in sources:
        for clause in re.split(r'[.;!?]|\bно\b', source):
            matches = list(re.finditer(purposes, clause))
            for index, match in enumerate(matches):
                before = clause[matches[index-1].end() if index else 0:match.start()]
                after = clause[match.end():matches[index+1].start() if index+1 < len(matches) else len(clause)]
                suffix = re.match(r'\s*[,—:-]?\s*' + negative, after)
                # A following preposition gives the denial its own object:
                # 'для бассейна, не подходит для дорог' denies roads only.
                suffix_denial = suffix and not re.match(
                    r'\s+(?:для|под)\b', clause[match.end()+suffix.end():])
                negated = bool(re.search(negative + r'(?:\s+\w+){0,3}\s*$', before)
                               or suffix_denial)
                is_pool = bool(re.match(r'бассейн|под|для', match.group()))
                if is_pool:
                    denied |= negated
                    pool |= not negated
                else:
                    other |= not negated
    unit = 'm2'
    for p in offer.findall('param') if offer is not None else []:
        if (p.get('name') or '').casefold().strip() in {'единица цены', 'единица продажи', 'единица измерения'}:
            value = (p.text or '').casefold().replace(' ', '')
            if value not in {'м²', 'м2', 'm2', 'm²', 'кв.м', 'кв.м.'}:
                unit = 'conflict'
    specs.update(kind='geotextile', geotextile={'purpose': 'denied' if denied else 'other' if other else 'pool' if pool else 'unknown', 'price_unit': unit})


def normalize(raw):
    from .pool_estimates import EstimateInputError
    if raw is None or raw == {}: return {}
    if not isinstance(raw, dict) or raw.get('purpose') not in {'pool','other','unknown','denied'} or raw.get('price_unit') not in {'m2','conflict'}:
        raise EstimateInputError('Некорректные характеристики геотекстиля.')
    return {key:raw[key] for key in ('purpose','price_unit')}


def assess(product):
    facts=product.get('geotextile') or {}
    if not product.get('available'): return 'Доступность товара не подтверждена.'
    if product.get('specification_conflict'): return 'Противоречивые характеристики товара.'
    if facts.get('price_unit') != 'm2': return 'Единица цены противоречит согласованной цене геотекстиля за м².'
    if facts.get('purpose') == 'denied': return 'Явно указано: геотекстиль не подходит для бассейна или под плёнку.'
    if facts.get('purpose') == 'other': return 'Указано другое назначение геотекстиля.'
    return None


def fill(snapshot, catalog, only_variant=None):
    if not snapshot.get('auxiliary_version'): return 0
    from .estimate_catalog import apply_product, refresh_auto_facts, select_price_tier
    count=0
    for tier, variant in snapshot['variants'].items():
        if only_variant and tier != only_variant: continue
        for block in variant['blocks']:
            if block['excluded']: continue
            for row in block['rows']:
                if (row['kind'] != 'geotextile' or row.get('finish_type') != 'film' or row['unit'] != 'м²'
                        or row['excluded']): continue
                origin=row.get('catalog')
                if origin:
                    current=catalog['products'].get(origin['id']) or origin
                    issue=assess(current)
                    row['catalog_review_note']='Текущая выгрузка: '+issue if issue else ''
                if row['existing'] or row.get('selection_locked') or row.get('unit_price_manual') or not row['auto_quantity']: continue
                auto=origin and origin['selection']=='auto' and row['price']==origin['price']
                if not auto and (origin or row['price'] is not None or row['model']): continue
                choices=[p for p in catalog['products'].values() if p['kind']=='geotextile' and not assess(p)]
                known=[p for p in choices if p['geotextile']['purpose']=='pool']
                chosen=select_price_tier(known or choices,tier)
                if chosen:
                    row['catalog_review_note']=''
                    if auto and chosen['id']==origin['id']: refresh_auto_facts(row,chosen,catalog)
                    else: apply_product(row,chosen,catalog,'auto');count+=1
                elif auto:
                    current=catalog['products'].get(origin['id']) or origin
                    issue=assess(current)
                    if issue:
                        row.update(name='Геотекстиль под плёнку (+15% запас)',model='',brand='',price=None,catalog=None,
                                   selection_note='Проверить: прежний автоматический товар отклонён. '+issue)
    return count


def annotate(snapshot, variant):
    if not snapshot.get('auxiliary_version'): return
    for block in variant['blocks']:
        if block['excluded']: continue
        for row in block['rows']:
            if row['kind']!='geotextile' or row['excluded'] or not row.get('catalog'): continue
            origin=row['catalog'];issue=assess(origin)
            note=row.get('catalog_review_note') or issue or ('Назначение и пригодность материала под плёнку бассейна не подтверждены.' if origin.get('geotextile',{}).get('purpose')!='pool' else '')
            row['price_conversion_note']='Цена каталога за м², без деления на рулон. Запас 15% уже включён в количество материала; повторно не начисляется.'
            if note:
                row['selection_note']='Проверить: '+note+' Источник: '+origin['url']+'; дата цены '+origin['checked_at']+'.'
                row['compatibility']={'message':row['selection_note'],'can_auto':False,'kind':'geotextile'}
                variant['warnings'].append(row['selection_note'])
