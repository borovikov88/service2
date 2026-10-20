"""Labor operations from the owner's Basic / Standart / Lux worksheet.

Only reusable operations and unit rates are stored here, never customer data or
old project quantities. Blank rates and project-specific package formulas stay
unknown. The concrete-preparation heading is not a chargeable operation.
"""

# code, block, name, unit, economy rate, standard/premium rate, quantity, optional
WORKS = (
    ('excavation', 'construction', 'Выемка грунта', 'м³', None, None, None, False),
    ('sand_base', 'construction', 'Песчаная подготовка', 'м²', None, '200', None, False),
    ('base_rebar', 'construction', 'Бетонная подготовка: армирование', 'м²', None, '300', None, False),
    ('base_concrete', 'construction', 'Бетонная подготовка: бетонирование', 'м³', None, '2400', None, False),
    ('brickwork', 'construction', 'Кирпичная кладка', 'м²', None, '1100', None, False),
    ('backfill', 'construction', 'Обратная засыпка', 'м³', None, '550', None, False),
    ('insulation', 'construction', 'Утепление', 'м²', None, '250', None, False),
    ('bowl_rebar', 'construction', 'Армирование чаши', 'м²', None, '300', None, False),
    ('angle_frame', 'construction', 'Каркас из уголка', 'м.п.', None, '300', None, False),
    ('formwork', 'construction', 'Устройство опалубки', 'м²', None, '400', None, False),
    ('bowl_concrete', 'construction', 'Бетонирование чаши', 'м³', None, '2000', None, False),
    ('strip_formwork', 'construction', 'Снятие опалубки', 'м²', None, '150', None, False),
    ('gutters', 'construction', 'Изготовление желобов', 'м.п.', None, '250', None, True),
    ('handling', 'construction', 'Погрузочно-разгрузочные работы (ПРР)', 'т', None, '300', None, False),
    ('construction_transport', 'construction', 'Транспортные расходы — строительство', 'компл.', None, None, '1', False),
    ('construction_travel', 'construction', 'Командировочные — строительство', 'чел./день', '2000', '2000', None, True),
    ('primer', 'finish', 'Грунтовка', 'м²', None, '70', 'finish_net', False),
    ('plaster', 'finish', 'Штукатурка', 'м²', None, '900', 'finish_net', False),
    ('finish_transport', 'finish', 'Транспортные расходы — отделка и монтаж', 'рейс', '2200', None, None, False),
    ('finish_travel', 'finish', 'Командировочные — отделка и монтаж', 'чел./день', None, None, None, True),
    ('countercurrent', 'options', 'Монтаж противотока', 'шт.', '9800', '9800', None, True),
    ('waterfall', 'options', 'Монтаж водопада', 'шт.', '9800', '9800', None, True),
)
LEGACY = {'bowl_work', 'embedded_work', 'equipment_work'}


def is_work(row):
    return row.get('kind') == 'work' or row['id'] in LEGACY or row['id'].startswith('finish_work')


def identify(row):
    if is_work(row):
        row['kind'] = 'work'
        if not row.get('work_code'):
            row['work_code'] = ('finish_' + (row.get('finish_type') or 'unknown')
                                if row['id'].startswith('finish_work') else row['id'])
    return row


def finish_rate(finish, variant):
    if finish == 'film':
        return '2000'
    if finish == 'mosaic':
        return '2000' if variant == 'economy' else '2500'
    return None


def finish_extras(finish, metrics, variant):
    from .pool_estimates import make_row
    rows = []
    if finish in ('film', 'tile', 'mosaic'):
        code = 'underlay' if finish == 'film' else 'waterproofing'
        row = make_row('work_' + code, 'Монтаж подложки' if finish == 'film' else 'Гидроизоляция',
                       'м²', metrics['finish_net'], 'finish_net', 'work')
        row.update(work_code=code + '_' + finish, finish_type=finish,
                   price=('200' if finish == 'film' else
                          ('200' if variant == 'economy' else '600') if finish == 'mosaic' else None))
        rows.append(row)
    if finish == 'film':
        row = make_row('work_liner_steps', 'Монтаж лайнера на ступени', 'шт.', None, kind='work')
        row.update(work_code='liner_steps', finish_type=finish, price='15000' if variant == 'economy' else None,
                   excluded=True, reason='Дополнительная работа: укажите количество ступеней и восстановите при необходимости')
        rows.append(row)
    return rows


def source_rate(row, variant):
    """Reusable unit rates only. Basic blanks may use the same work/unit in Standard.

    Source: owner's workbook, basic/standart/lux G43:G59 and G158:G166,
    Оборудование G148:G153. Never treat cached package totals as unit rates.
    """
    code = row.get('work_code')
    for item_code, _, _, unit, economy, standard, _, _ in WORKS:
        if item_code != code:
            continue
        if code == 'finish_transport' and variant != 'economy':
            unit = 'компл.'
        if row['unit'] != unit:
            return None
        return (economy if economy is not None else standard) if variant == 'economy' else standard
    if code == 'equipment_work' and row['unit'] == 'компл.' and variant == 'economy':
        return '70000'
    if code == 'liner_steps' and row['unit'] == 'шт.' and row.get('finish_type') == 'film':
        return '15000' if variant == 'economy' else None
    finish = row.get('finish_type')
    if row['unit'] == 'м²' and finish in ('film', 'mosaic'):
        if code == 'finish_' + finish:
            return finish_rate(finish, variant)
        if code == ('underlay_film' if finish == 'film' else 'waterproofing_mosaic'):
            return '200' if finish == 'film' or variant == 'economy' else '600'
    return None


def fill_missing_prices(snapshot):
    count = 0
    for key, variant in snapshot['variants'].items():
        for block in variant['blocks']:
            for row in block['rows']:
                if not is_work(row) or row['price'] is not None or row['existing'] or row.get('selection_locked') or row.get('rate_source') == 'Ручная цена текущей сметы':
                    continue
                rate = source_rate(row, key)
                if rate is not None:
                    row['price'] = rate
                    count += 1
    return count


def add_works(snapshot, *, fresh=False):
    """Explicit upgrade of an editable copy. Never alter existing rates or history."""
    from .pool_estimates import make_row, geometry, EstimateInputError
    from .estimate_technology import bind_new_work
    metrics = geometry(snapshot['inputs'])
    for key, variant in snapshot['variants'].items():
        blocks = {b['id']: b for b in variant['blocks']}
        for block in blocks.values():
            for row in block['rows']:
                identify(row)
        generic = next((r for r in blocks['construction']['rows'] if r['id'] == 'bowl_work'), None)
        for code, bid, name, unit, economy, standard, qty, optional in WORKS:
            block = blocks[bid]
            if any(r.get('work_code') == code for r in block['rows']):
                continue
            basis = qty if qty in metrics else ''
            # Standart/Lux use a package, Basic a trip: never propagate across units.
            if code == 'finish_transport' and key != 'economy':
                unit, qty = 'компл.', '1'
            row = make_row('work_' + code, name, unit, metrics[basis] if basis else qty, basis, 'work')
            row.update(work_code=code)
            bind_new_work(row)
            row['price'] = source_rate(row, key)
            if optional or (generic and bid == 'construction'):
                row.update(excluded=True, reason='Есть общая строка строительства: замените её детализацией при необходимости' if generic and bid == 'construction' else 'Дополнительная работа: восстановите при необходимости')
            if len(block['rows']) >= 100:
                raise EstimateInputError('В блоке недостаточно места для работ из Excel.')
            ids = {r['id'] for b in blocks.values() for r in b['rows']}
            if row['id'] in ids:
                base = row['id']
                row['id'] = next(f'{base}_{n}' for n in range(501) if f'{base}_{n}' not in ids)
            block['rows'].append(row)
        if fresh:
            for bid, rid, price in (('equipment', 'equipment_work', '70000' if key == 'economy' else None),
                                    ('finish', 'finish_work', finish_rate(snapshot['inputs']['finish'], key))):
                row = next(r for r in blocks[bid]['rows'] if r['id'] == rid)
                row['price'] = price
    if fresh:
        fill_missing_prices(snapshot)
    snapshot['works_catalog'] = 1
    return snapshot


def shared_base(row):
    if row.get('unit') == 'м²' and row.get('finish_type') == 'film':
        return {'finish_film':'2000', 'underlay_film':'200'}.get(row.get('work_code'))
    return None
