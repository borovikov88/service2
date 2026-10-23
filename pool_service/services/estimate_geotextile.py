"""Geotextile units: confirmed 26930 roll is 2 × 50 m; other prices stay per m²."""
import re

PREFERRED_URL="https://shop.aqualine22.ru/basseini/297-otdelochnye-materialy/307-lajner/geotekstil-aquaviva-300-gm2-50-m.html"
from html import unescape
from copy import deepcopy
from decimal import Decimal, InvalidOperation

# Supplier SKU 26930: https://aquapolis.ru/geotekstil-aquaviva-300-g-m2-50-m.html
# 2 m × 50 m = 100 m², price per roll. No price is hardcoded.
def confirmed_roll(name, sku, url):
    return url == PREFERRED_URL or (sku == "26930" and "aquaviva" in name.casefold() and name.casefold().startswith("геотекстиль"))


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
                short_denial = re.search(r'\bне\s+(?:(?:для|под)\s+)?$', before)
                negated = bool(short_denial or re.search(negative + r'(?:\s+\w+){0,3}\s*$', before)
                               or suffix_denial)
                is_pool = bool(re.match(r'бассейн|под|для', match.group()))
                if is_pool:
                    denied |= negated
                    pool |= not negated
                else:
                    other |= not negated
    known_roll = offer is not None and confirmed_roll(name, offer.findtext('vendorCode', ''), offer.findtext('url', ''))
    unit = 'roll' if known_roll else 'm2'
    for p in offer.findall('param') if offer is not None else []:
        if (p.get('name') or '').casefold().strip() in {'единица цены', 'единица продажи', 'единица измерения'}:
            value = (p.text or '').casefold().replace(' ', '')
            if value in {'м²', 'м2', 'm2', 'm²', 'кв.м', 'кв.м.'}:
                if known_roll: unit = 'conflict'
            elif value in {'рулон', 'рул.', 'рул'}:
                if not known_roll: unit = 'conflict'
            else: unit = 'conflict'
    if known_roll:
        for param in offer.findall('param'):
            key=(param.get('name') or '').casefold().strip()
            expected = '2' if key in {'ширина рулона', 'ширина рулона, м'} else '50' if key in {'длина рулона', 'длина рулона, м'} else '100' if key in {'площадь рулона', 'площадь рулона, м²', 'площадь рулона, м2'} else None
            if expected:
                value=re.sub(r'\s*(?:м²|м2|м)\s*$', '', (param.text or '').strip()).replace(',', '.')
                try:
                    if Decimal(value) != Decimal(expected): unit='conflict'
                except InvalidOperation: unit='conflict'
    if known_roll:
        from .estimate_liner_facts import metres
        n=r'(\d+(?:[.,]\d+)?)'
        for index, source in enumerate(sources[:2]):
            for field, expected in (('ширина',Decimal('2')),('длина',Decimal('50'))):
                for m in re.finditer(field+r'\s+рулона\s*[:=]?\s*'+n+r'\s*(мм|см|м)\b',source):
                    if metres(m[1]+m[2]) != expected: unit='conflict'
            for m in re.finditer(r'(?:площадь рулона|в рулоне)\s*[:=]?\s*'+n+r'\s*(?:м²|м2)\b',source):
                if Decimal(m[1].replace(',','.')) != Decimal('100'): unit='conflict'
            for m in re.finditer(r'(?<![\d.,xх×])'+n+r'\s*[xх×*]\s*'+n+r'\s*(мм|см|м)\b',source):
                if re.search(r'[xх×*]\s*$',source[:m.start()]) or re.match(r'\s*[xх×*]',source[m.end():]):continue
                if index and not re.search(r'(?:размер\w*\s+рулона|размер\w*\s*[:=])\s*[:=]?\s*$',source[:m.start()]):continue
                dimensions=sorted([metres(m[1]+m[3]),metres(m[2]+m[3])])
                if dimensions != [Decimal('2'),Decimal('50')]:unit='conflict'
    specs.update(kind='geotextile', geotextile={'purpose': 'denied' if denied else 'pool' if pool else 'other' if other else 'unknown', 'price_unit': unit, **({'area': '100'} if known_roll else {})})


def normalize(raw):
    from .pool_estimates import EstimateInputError
    if raw is None or raw == {}: return {}
    if not isinstance(raw, dict) or raw.get('purpose') not in {'pool','other','unknown','denied'} or raw.get('price_unit') not in {'m2','roll','conflict'}:
        raise EstimateInputError('Некорректные характеристики геотекстиля.')
    result = {key:raw[key] for key in ('purpose','price_unit')}
    if raw.get('area') is not None:
        if str(raw['area']) != '100': raise EstimateInputError('Неподтверждённая площадь рулона геотекстиля.')
        result['area'] = '100'
    if result['price_unit'] == 'roll' and not result.get('area'):
        raise EstimateInputError('Не указана площадь рулона геотекстиля.')
    return result


def assess(product):
    facts=product.get('geotextile') or {}
    if not product.get('available'): return 'Доступность товара не подтверждена.'
    if product.get('specification_conflict'): return 'Противоречивые характеристики товара.'
    if facts.get('price_unit') not in {'m2','roll'}: return 'Единица цены противоречит согласованной цене геотекстиля за м².'
    if facts.get('purpose') == 'denied': return 'Явно указано: геотекстиль не подходит для бассейна или под плёнку.'
    if facts.get('purpose') == 'other': return 'Указано другое назначение геотекстиля.'
    return None


def fill(snapshot, catalog, only_variant=None):
    if not snapshot.get('auxiliary_version'): return 0
    from .estimate_catalog import apply_product, select_price_tier
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
                preferred=[p for p in choices if p['url']==PREFERRED_URL]
                chosen=select_price_tier(preferred or known or choices,tier)
                if chosen:
                    row['catalog_review_note']=''
                    if auto and chosen['id']==origin['id']:
                        # Keep the frozen source price. An explicit recalculation may
                        # correct old automatic per-m² interpretation of this roll;
                        # evaluate/load alone never reprices historical snapshots.
                        frozen_product=deepcopy(chosen)
                        frozen_product['price']=origin.get('roll_price',origin['price'])
                        frozen_catalog={**catalog,'checked_at':origin['checked_at'],'feed_date':origin['feed_date']}
                        apply_product(row,frozen_product,frozen_catalog,'auto')
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
            from .estimate_liner import note as roll_note
            row['price_conversion_note']=((roll_note(row) or 'Ручная цена за единицу строки; автоматический пересчёт цены рулона отключён.') if origin.get('roll_area') else 'Цена каталога за м², без деления на рулон.') + ' Запас 15% уже включён в количество материала; повторно не начисляется.'
            if note:
                row['selection_note']='Проверить: '+note+' Источник: '+origin['url']+'; дата цены '+origin['checked_at']+'.'
                row['compatibility']={'message':row['selection_note'],'can_auto':False,'kind':'geotextile'}
                variant['warnings'].append(row['selection_note'])
