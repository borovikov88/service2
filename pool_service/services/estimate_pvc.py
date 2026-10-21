"""Owner-approved preliminary materials allowance, final rates without markup."""
from decimal import Decimal, ROUND_HALF_UP

ROWS = {
    'pvc_embedded': ('embedded', 'ПВХ: трубы и фитинги закладных', Decimal('1000')),
    'piping': ('equipment', 'ПВХ: обвязка оборудования', Decimal('1200')),
}


def initialize(snapshot):
    from .pool_estimates import make_row
    snapshot['pvc_area_version'] = 1
    for variant in snapshot['variants'].values():
        for identity, (block_id, title, _) in ROWS.items():
            block = next(b for b in variant['blocks'] if b['id'] == block_id)
            row = next((r for r in block['rows'] if r['id'] == identity), None)
            if row is None:
                row = make_row(identity, title)
                block['rows'].append(row)
            else:
                row['name'] = title  # Reuse the fresh equipment allowance, never duplicate it.
    apply(snapshot)


def apply(snapshot):
    if snapshot.get('pvc_area_version') != 1:
        return
    area = Decimal(snapshot['inputs']['length']) * Decimal(snapshot['inputs']['width'])
    for variant in snapshot['variants'].values():
        for block in variant['blocks']:
            for row in block['rows']:
                if row['id'] not in ROWS or ROWS[row['id']][0] != block['id']:
                    continue
                # Explicit catalogue selection uses the catalogue price lifecycle.
                # Its frozen price is not a manual override of the area allowance.
                if row.get('catalog') or row.get('existing'):
                    row['construction_quantity_note'] = ('Выбран товар каталога; укрупнённая оценка ПВХ по площади не применяется.'
                        if row.get('catalog') else '')
                    continue
                rate = ROWS[row['id']][2]
                computed = str((area * rate).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
                previous = row.get('pvc_computed_price')
                if previous is not None and row['price'] != previous and (not row.get('pvc_price_suspended') or row['price'] is not None):
                    row['unit_price_manual'] = True  # Includes explicit zero and cleared price.
                protected = row.get('unit_price_manual') or row.get('catalog') or row.get('existing')
                if not protected:
                    row['price'] = computed if row['unit'] == 'компл.' else None
                    row['pvc_price_suspended'] = row['unit'] != 'компл.'
                row['pvc_computed_price'] = computed
                row['construction_quantity_note'] = (
                    f'Предварительная укрупнённая оценка по зеркалу воды: {area} м² × {rate} ₽/м² = {computed} ₽ за 1 комплект. '
                    'Только материалы, без работ и дополнительных 20%. Фактический состав и трасса уточняются.'
                    + (' Сохранена ручная цена.' if row.get('unit_price_manual') else '')
                    + (' Проверьте единицу: расчётная цена предназначена за комплект.' if row['unit'] != 'компл.' else ''))
