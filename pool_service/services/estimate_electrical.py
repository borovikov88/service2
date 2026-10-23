"""User-approved equipment allowance, with frozen/manual price protection."""

ID = 'electrical_equipment'
PRICES = {'230': '15000.00', '400': '20000.00'}


def initialize(snapshot):
    if snapshot.get('electrical_version') == 1:
        return
    from .pool_estimates import make_row
    for variant in snapshot['variants'].values():
        if any(r['id'] == ID for b in variant['blocks'] for r in b['rows']):
            continue
        block = next(b for b in variant['blocks'] if b['id'] == 'equipment')
        from .pool_estimates import EstimateInputError
        if len(block['rows']) >= 100:
            raise EstimateInputError('Недостаточно места для электрооборудования.')
        row = make_row(ID, 'Электрооборудование')
        row.update(electrical_managed=True, price=PRICES.get(snapshot['inputs']['power']),
                   electrical_computed_price=PRICES.get(snapshot['inputs']['power']))
        block['rows'].append(row)
    snapshot['electrical_version'] = 1


def apply(snapshot):
    if snapshot.get('electrical_version') != 1:
        return
    computed = PRICES.get(snapshot['inputs']['power'])
    for variant in snapshot['variants'].values():
        for block in variant['blocks']:
            for row in block['rows']:
                if row['id'] != ID or block['id'] != 'equipment' or not row.get('electrical_managed') or row.get('catalog') or row['existing']:
                    continue
                if row['price'] != row.get('electrical_computed_price') and (not row.get('electrical_price_suspended') or row['price'] is not None):
                    row['unit_price_manual'] = True
                if not row.get('unit_price_manual'):
                    row['price'] = computed if row['unit'] == 'компл.' else None
                    row['electrical_price_suspended'] = row['unit'] != 'компл.'
                row['electrical_computed_price'] = computed
                row['selection_note'] = ('1 комплект: 220 В — 15 000 ₽, 380 В — 20 000 ₽. '
                    + ('Проверить: питание не указано, цена не включена в итог.' if computed is None else '')
                    + (' Сохранена ручная цена.' if row.get('unit_price_manual') else ''))
