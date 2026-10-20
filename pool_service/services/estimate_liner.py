"""Owner-confirmed roll units and exact series; prices always come from the feed."""
from decimal import Decimal as D, ROUND_HALF_UP
import re

# SKU -> tier, area of one roll. No roll purchasing/cutting optimization.
ROLLS = {'34500': ('economy', '41.58'), '34501': ('economy', '51.66'),
         '24920': ('standard', '41.58'), '24921': ('standard', '51.66'),
         '30528': ('premium', '41.25')}


def area(product):
    fact = ROLLS.get(product.get('sku'))
    if not fact: return None
    name = product['name'].casefold()
    patterns = {'economy': r'aquaviva.*azure\s+blue.*акрил',
                'standard': r'cefil.*france.*голуб', 'premium': r'cefil.*touch.*onyx\s+hawai'}
    return fact[1] if re.search(patterns[fact[0]], name) else None


def validate_selection(row, product):
    from .pool_estimates import EstimateInputError
    is_liner = (product.get('sku') in ROLLS or bool(re.search(r'^(?:(?:армированная|пвх)\s+)*(?:лайнер|пл[её]нка)\b', product['name'], re.I)))
    film_target = row['id'].startswith('finish_material') and row.get('finish_type') == 'film'
    if not is_liner and not film_target: return
    if not area(product):
        raise EstimateInputError('Для этой плёнки не подтверждена площадь и единица продажи рулона. Уточните единицу и задайте проверенную цену вручную.')
    if row['unit'] not in ('м²', 'рулон', 'рул.'):
        raise EstimateInputError('Цена выбранной плёнки указана за рулон. Выберите единицу м² для пересчёта либо рулон; цена в другую единицу не переносится.')


def set_conversion(row, product):
    size = area(product)
    if row['unit'] != 'м²' or not size:
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
    if not active(row): return ''
    origin = row['catalog']
    return (f"Цена рулона {origin['roll_price']} ₽ / {origin['roll_area']} м². Цена за м² показана округлённо; "
            'сумма рассчитана без промежуточного округления. Количество в м², закупка целыми рулонами и раскрой не рассчитаны.')


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
                choices = [p for p in catalog['products'].values() if p['available'] and area(p) and ROLLS[p['sku']][0] == tier]
                if not choices: continue
                # Stable narrow roll first; this is not a cutting or waste optimizer.
                product = min(choices, key=lambda p:(D(area(p)), int(p['id'])))
                apply_product(row, product, catalog, 'auto')
                count += 1
    return count
