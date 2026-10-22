"""Owner's finishing allowance: exact internal floor/walls, rounded once."""
from decimal import Decimal, ROUND_CEILING

ID = 'finish_consumables'
RATE = Decimal('350')


def values(snapshot):
    inputs = snapshot['inputs']
    length, width = (Decimal(inputs[k]) for k in ('length', 'width'))
    if inputs.get('bowl_height'):
        area = length * width + 2 * (length + width) * Decimal(inputs['bowl_height'])
    else:
        # Preserve the established sloped-floor geometry of legacy estimates.
        from .pool_estimates import geometry
        area = Decimal(geometry(inputs)['finish_area'])
    price = (area * RATE / 1000).to_integral_value(rounding=ROUND_CEILING) * 1000
    return area, format(price, '.2f')


def initialize(snapshot):
    if snapshot.get('consumables_version') == 1:
        return
    from .pool_estimates import make_row, EstimateInputError
    _, price = values(snapshot)
    for variant in snapshot['variants'].values():
        if any(r['id'] == ID for b in variant['blocks'] for r in b['rows']):
            continue  # A pre-existing manual row is not a blank default.
        block = next(b for b in variant['blocks'] if b['id'] == 'finish')
        if len(block['rows']) >= 100:
            raise EstimateInputError('Недостаточно места для расходных материалов.')
        row = make_row(ID, 'Расходные материалы для отделки')
        row.update(price=price, consumables_computed_price=price)
        block['rows'].append(row)
    snapshot['consumables_version'] = 1


def apply(snapshot):
    if snapshot.get('consumables_version') != 1:
        return
    area, computed = values(snapshot)
    for variant in snapshot['variants'].values():
        for block in variant['blocks']:
            for row in block['rows']:
                previous = row.get('consumables_computed_price')
                if row['id'] != ID or block['id'] != 'finish' or previous is None:
                    continue
                if row.get('catalog') or row.get('existing'):
                    continue  # Catalogue refresh/frozen prices have their own lifecycle.
                if row['price'] != previous and (not row.get('consumables_price_suspended') or row['price'] is not None):
                    row['unit_price_manual'] = True
                if not row.get('unit_price_manual'):
                    row['price'] = computed if row['unit'] == 'компл.' else None
                    row['consumables_price_suspended'] = row['unit'] != 'компл.'
                row['consumables_computed_price'] = computed
                row['construction_quantity_note'] = (
                    f'Дно и внутренние стены: {area} м² × 350 ₽/м²; округление суммы вверх до 1 000 ₽: {computed} ₽ за комплект. '
                    'Без запаса, верхнего захода плёнки, работ и дополнительных 20%.'
                    + (' Сохранена ручная цена.' if row.get('unit_price_manual') else '')
                    + (' Проверьте единицу: цена рассчитана за комплект.' if row['unit'] != 'компл.' else ''))
