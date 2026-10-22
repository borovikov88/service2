"""Owner-confirmed roll units and exact series; prices always come from the feed."""
from decimal import Decimal as D, ROUND_HALF_UP, ROUND_CEILING
import re

# SKU -> tier, area of one roll. No roll purchasing/cutting optimization.
ROLLS = {'34500': ('economy', '41.58'), '34501': ('economy', '51.66'),
         '24920': ('standard', '41.58'), '24921': ('standard', '51.66'),
         '30528': ('premium', '41.25')}


def area(product):
    geo=product.get('geotextile') or {}
    if geo.get('price_unit')=='roll':return geo.get('area')
    facts=product.get('liner') or {}
    if facts.get('conflict') or facts.get('wrong_purpose'):return None
    if facts.get('area'):return facts['area']
    fact = ROLLS.get(product.get('sku'))
    if not fact: return None
    name = product['name'].casefold()
    patterns = {'economy': r'aquaviva.*azure\s+blue.*акрил',
                'standard': r'cefil.*france.*голуб', 'premium': r'cefil.*touch.*onyx\s+hawai'}
    return fact[1] if re.search(patterns[fact[0]], name) else None


def validate_selection(row, product):
    from .pool_estimates import EstimateInputError
    if product.get('kind')=='geotextile' and product.get('geotextile',{}).get('area') and row['unit'] not in ('м²','рулон','рул.'):
        raise EstimateInputError('Для геотекстиля с ценой за рулон выберите м² для пересчёта либо рулон.')
    is_liner = (product.get('kind')=='liner' or product.get('sku') in ROLLS or bool(re.search(r'^(?:(?:армированная|пвх)\s+)*(?:лайнер|пл[её]нка)\b', product['name'], re.I)))
    film_target = row['id'].startswith('finish_material') and row.get('finish_type') == 'film'
    if not is_liner and not film_target: return
    if film_target and not is_liner:
        raise EstimateInputError('В строку плёнки выбирается лайнер, а не сопутствующий материал.')
    facts=product.get('liner') or {}
    if facts.get('wrong_purpose'):
        raise EstimateInputError('В карточке указано другое назначение плёнки; применение в чаше не подтверждено.')
    if row['unit'] in ('рулон','рул.') and (facts.get('price_unit')=='m2' or facts.get('unit_explicit') and facts.get('price_unit')=='unknown'):
        raise EstimateInputError('Единица цены каталога не рулон. Выберите м² для цены за м²; другую единицу подтвердите вручную.')
    if row['unit'] not in ('м²', 'рулон', 'рул.'):

        raise EstimateInputError('Цена выбранной плёнки указана за рулон. Выберите единицу м² для пересчёта либо рулон; цена в другую единицу не переносится.')


def set_conversion(row, product):
    size = area(product)
    facts=product.get('liner') or {}
    if row['unit'] != 'м²':return
    if facts.get('price_unit')=='m2' and not facts.get('conflict'):
        return
    if product.get('kind')=='geotextile' and product.get('geotextile',{}).get('price_unit')=='conflict':
        row['price']=None
        row['selection_note']='Проверить: противоречивые размеры или единица цены геотекстиля; цена за м² не определена.'
        return
    if not size or facts.get('unit_explicit') and facts.get('price_unit')=='unknown':
        if product.get('kind')=='liner':
            row['price']=None
            row['selection_note']='Проверить: площадь рулона или единица продажи не подтверждены. Цена рулона не перенесена в цену за м²; можно задать проверенную цену за м² вручную.'
        return
    origin = row['catalog']
    origin.update(roll_price=product['price'], roll_area=size)
    origin['price'] = str((D(product['price'])/D(size)).quantize(D('.01'), rounding=ROUND_HALF_UP))
    row.update(price=origin['price'], unit_price_manual=False)


def active(row):
    origin = row.get('catalog') or {}
    return (origin.get('roll_price') is not None and origin.get('roll_area') and
            row['unit'] == 'м²' and not row.get('unit_price_manual') and
            row['price'] is not None and D(row['price']) == D(origin['price']))


def amount(row):
    origin = row['catalog']
    # Multiply before division; never multiply the displayed rounded m² price.
    return (D(row['quantity'])*D(origin['roll_price'])/D(origin['roll_area'])).quantize(D('.01'), rounding=ROUND_HALF_UP)


def note(row):
    if not active(row):
        if row.get('kind')=='liner' and row.get('catalog'):
            if row.get('unit_price_manual'):return 'Ручная цена за единицу строки; автоматический пересчёт цены рулона отключён.'
            product={**row['catalog'],'name':row['name']}
            size=area(product)
            if product.get('liner',{}).get('price_unit')=='m2' and size and row['quantity'] is not None:
                rolls=(D(row['quantity'])/D(size)).to_integral_value(rounding=ROUND_CEILING)
                return f'Цена каталога за м². Рулон {size} м²; справочно {rolls} целых рулонов, без увеличения стоимости нужных м². Раскрой требует проверки.'
            return search_note(product)
        return ''
    origin = row['catalog']
    rolls=(D(row['quantity'])/D(origin['roll_area'])).to_integral_value(rounding=ROUND_CEILING) if row['quantity'] is not None else None
    return (f"Цена рулона {origin['roll_price']} ₽ / {origin['roll_area']} м². Цена за м² показана округлённо; "
            'сумма рассчитана без промежуточного округления. Количество в м². '
            + (f'Справочно: {rolls} целых рулонов = {rolls*D(origin["roll_area"])} м². ' if rolls is not None else '')
            + 'Стоимость только нужных м²; справочные рулоны не увеличивают сумму. Раскрой и сварные швы требуют проверки.')


def fill(snapshot, catalog, only_variant=None):
    from .estimate_catalog import apply_product
    count = 0
    for tier, variant in snapshot['variants'].items():
        if only_variant and tier != only_variant: continue
        for block in variant['blocks']:
            if block['excluded']: continue
            for row in block['rows']:
                if (not row['id'].startswith('finish_material') or row.get('finish_type') != 'film' or row['unit'] != 'м²'
                        or row['excluded'] or row['existing'] or row.get('selection_locked') or row.get('unit_price_manual')): continue
                origin = row.get('catalog')
                if origin: continue  # Frozen existing selections (including older snapshots) stay intact.
                if row['price'] is not None or row['model']: continue
                choices = [p for p in catalog['products'].values() if p['available'] and area(p) and ROLLS.get(p['sku'],('',))[0] == tier]
                if not choices: continue
                # Stable narrow roll first; this is not a cutting or waste optimizer.
                product = min(choices, key=lambda p:(D(area(p)), int(p['id'])))
                apply_product(row, product, catalog, 'auto')
                count += 1
    return count


def search_note(product):
    facts=product.get('liner') or {}
    if facts.get('conflict'):return 'Проверить: противоречивые размеры/единица продажи рулона. Автоматический пересчёт цены приостановлен.'
    if facts.get('wrong_purpose'):return 'Проверить: в карточке указано иное назначение плёнки; применение для чаши не подтверждено.'
    size=area(product)
    if facts.get('unit_explicit') and facts.get('price_unit')=='unknown':return 'Проверить: единица цены каталога не подтверждает цену рулона или м². Автоматический пересчёт цены отключён.'
    if facts.get('price_unit')=='m2':return 'Цена каталога за м²; площадь рулона нужна только для справочного количества рулонов.'
    if size:return 'Рулон '+str(size)+' м²; цена пересчитывается в м². Целые рулоны показываются справочно, без увеличения стоимости.'
    return 'Проверить: размер рулона/единица продажи не подтверждены. Товар можно выбрать; цена за м² требует уточнения и не будет подменена ценой рулона.'
