"""Opt-in plaster material systems; prices and contact-layer consumption are unknown.

Source dosages describe consumed dry mix, never the rounded purchased bag count.
No catalogue prices, latex densities or contact-layer coverage are inferred here.
"""
from decimal import Decimal as D, ROUND_CEILING

from .estimate_technology import OFF, mode

SYSTEMS = {
    'standard': {
        'mix': 'Litokol CR30', 'latex': 'IDROKOL X20', 'unit': 'кг',
        'dose': D('.9'), 'minimum_mm': D('8'),
        'source': 'https://www.litokol.ru/catalog/litokol-cr30/',
        'contact_source': 'https://www.litokol.ru/catalog/idrokol-x20/',
        'contact': 'Контактное молочко IDROKOL X20 : цемент : вода = 1:1:1 по массе. Расход на м² не подтверждён.',
    },
    'premium': {
        'mix': 'Mapei Nivoplan Plus', 'latex': 'Admix P', 'unit': 'л',
        'dose': D('.5'), 'minimum_mm': None,
        'source': 'https://aomapei.ru/upload/iblock/d49/34sb8fchfjxi0wxdwcf4fo4eym3weygn.pdf',
        'contact_source': 'https://aomapei.ru/upload/iblock/75a/yrpby89fgazkmuzooa1ggkyap3hg5x1j/TDS_Admix%20P_12102026.pdf',
        'contact': 'Контактный слой: 2 л Admix P на 25 кг Nivoplan Plus. Расход смеси на м² не подтверждён.',
    },
}
EXPECTED_UNITS = {'plaster_mix_bags': 'меш.', 'plaster_latex_kg': 'кг',
                  'plaster_latex_l': 'л', 'plaster_contact': 'компл.',
                  'plaster_wall_bags': 'меш.', 'plaster_floor_bags': 'меш.'}
BASES = set(EXPECTED_UNITS)
IDS = ('tech_plaster_material', 'tech_plaster_latex', 'tech_plaster_contact')


def metrics(inputs, tier):
    """Exact net/stock quantities, with purchasing rounding only at the last step."""
    system = SYSTEMS[tier]
    dimensions = [inputs.get(k) for k in ('length', 'width', 'bowl_height')]
    area = None
    if all(value not in (None, '') for value in dimensions):
        length, width, height = map(D, dimensions)
        area = 2 * (length + width) * height
        if mode(inputs, 'plaster') == 'walls_floor':
            area += length * width
    thickness = inputs.get('tech_plaster_thickness')
    result = dict.fromkeys(('area', 'thickness_mm', 'mix_net_kg', 'mix_stock_kg',
                           'mix_purchase_bags', 'latex_net', 'latex_stock', 'latex_quantity'))
    if area is None or thickness in (None, '') or mode(inputs, 'plaster') not in ('walls', 'walls_floor'):
        return result
    area, mm = D(area), D(thickness) * 1000
    result.update(area=area, thickness_mm=mm)
    if mm <= 0 or mm > 30 or system['minimum_mm'] is not None and mm < system['minimum_mm']:
        return result
    net = area * mm * D('1.6')
    latex = net / 25 * system['dose']
    result.update(mix_net_kg=net, mix_stock_kg=net * D('1.05'),
                  mix_purchase_bags=(net * D('1.05') / 25).to_integral_value(rounding=ROUND_CEILING),
                  latex_net=latex, latex_stock=latex * D('1.05'),
                  latex_quantity=(latex * D('1.05')).quantize(D('.001'), rounding=ROUND_CEILING))
    return result


def _untouched_placeholder(row):
    return (row.get('basis') == 'tech_plaster_volume' and row.get('unit') == 'м³'
            and row.get('auto_quantity') and not row.get('existing')
            and not row.get('excluded') and not row.get('catalog')
            and row.get('price') is None and not row.get('unit_price_manual')
            and not row.get('selection_locked'))


def initialized_tiers(snapshot):
    """Persist per-quote initialization so deleting components is not an instruction to rebuild."""
    initialized = set(snapshot.get('plaster_initialized', []))
    if snapshot.get('plaster_systems') == 1:
        for tier, variant in snapshot.get('variants', {}).items():
            if any(r.get('basis') in BASES for b in variant['blocks'] for r in b['rows']):
                initialized.add(tier)
        snapshot['plaster_initialized'] = sorted(initialized)
    return initialized


def _can_initialize(snapshot, tier, create):
    return (create and tier not in initialized_tiers(snapshot)
            and mode(snapshot['inputs'], 'plaster') in ('walls', 'walls_floor'))


def apply(snapshot, create=False):
    """Update opted-in rows and return per-variant warnings.

    Call after technology and construction annotations. Initialize on first activation only; subsequently deleted component rows stay deleted.
    """
    warnings = {tier: [] for tier in snapshot.get('variants', {})}
    if snapshot.get('plaster_systems') != 1:
        return warnings
    from .pool_estimates import make_row
    for tier, system in SYSTEMS.items():
        variant = snapshot.get('variants', {}).get(tier)
        if variant is None:
            continue
        rows = [r for b in variant['blocks'] for r in b['rows']]
        original = next((r for r in rows if r['id'] == IDS[0]), None)
        if _can_initialize(snapshot, tier, create) and original is not None and _untouched_placeholder(original):
            original.update(name=system['mix'] + ' — основной штукатурный слой, мешок 25 кг',
                            unit='меш.', basis='plaster_mix_bags', quantity=None)
            block = next(b for b in variant['blocks'] if any(r is original for r in b['rows']))
            insertion = block['rows'].index(original) + 1
            for identity, name, unit, basis in (
                (IDS[1], system['latex'] + ' — добавка основного слоя', system['unit'],
                 'plaster_latex_kg' if tier == 'standard' else 'plaster_latex_l'),
                (IDS[2], system['mix'] + ' — контактный слой (уточнить расход)', 'компл.', 'plaster_contact'),
            ):
                if not any(r['id'] == identity for r in rows):
                    block['rows'].insert(insertion, make_row(identity, name, unit, None, basis))
                    insertion += 1
            rows = [r for b in variant['blocks'] for r in b['rows']]
        active = [r for r in rows if r['id'] in IDS and r.get('basis') in BASES]
        if not active:
            if original is not None and mode(snapshot['inputs'], 'plaster') != 'off':
                warnings[tier].append('Штукатурка: сохранена исходная ручная строка; система материалов автоматически не добавлена во избежание двойного начисления.')
            continue
        values = metrics(snapshot['inputs'], tier)
        state = mode(snapshot['inputs'], 'plaster')
        for row in active:
            basis = row['basis']
            if state == 'off':
                if not row.get('excluded'):
                    row.update(excluded=True, reason=OFF)
                continue
            if row.get('excluded') and row.get('reason') == OFF:
                row.update(excluded=False, reason='')
            if row.get('auto_quantity') and not row.get('existing'):
                value = None
                if row.get('unit') == EXPECTED_UNITS[basis]:
                    value = values['mix_purchase_bags'] if basis == 'plaster_mix_bags' else values['latex_quantity'] if basis.startswith('plaster_latex_') else None
                else:
                    warnings[tier].append(f"{row['name']}: расчётная единица {EXPECTED_UNITS[basis]}, в строке {row.get('unit')}; автоматическое количество не подтверждено.")
                row['quantity'] = format(value, 'f') if value is not None else None
            if values['mix_net_kg'] is not None:
                note = (f"Площадь {values['area']} м²; слой {values['thickness_mm']} мм. "
                        f"Основная смесь без запаса {values['mix_net_kg']} кг; запас 5%: {values['mix_stock_kg']} кг; "
                        f"закупка {values['mix_purchase_bags']} меш. × 25 кг. "
                        f"Добавка по фактическому расходу смеси: {values['latex_net']} {system['unit']}; "
                        f"с запасом 5%: {values['latex_stock']} {system['unit']} (округление вверх до 0,001). ")
            else:
                note = 'Количество не подтверждено: проверьте площадь и толщину слоя; допустимый максимум 30 мм.'
                if tier == 'standard':
                    note += ' CR30: минимум 8 мм.'
            if basis == 'plaster_contact':
                note = system['contact'] + ' Количество и стоимость не определены; это не нулевой расход.'
            if tier == 'premium' and basis == 'plaster_latex_l':
                note += ' Фасовка Admix P в кг: плотность не подтверждена, перевод литров в кг и упаковки не выполнен.'
            source = system['contact_source'] if basis == 'plaster_contact' else system['source']
            row['construction_quantity_note'] = note + ' Источник: ' + source
        if state != 'off':
            warnings[tier].append(f"Штукатурка {system['mix']}: проверить расход контактного слоя и цены компонентов; контактный слой не включён в расчётное количество основного слоя.")
            if values['mix_net_kg'] is None:
                warnings[tier].append('Штукатурка: автоматический расход не подтверждён для заданной геометрии/толщины.')
            if tier == 'premium':
                warnings[tier].append('Admix P: дозировка в литрах; закупка в кг требует подтверждённой плотности.')
    warnings['economy'] = _apply_economy(snapshot, create)
    initialized_tiers(snapshot)
    return warnings


ECONOMY = {
    'plaster_wall_bags': ('Геркулес GP-21 — стены, мешок 25 кг', D('1.67'), D(25), D(5), D(30),
        'https://gerkules.com/catalog/shtukaturki/detail/shtukaturka-tsementnaya-gp-21-gp-21z-zimnyaya/'),
    'plaster_floor_bags': ('Геркулес GF-17 — дно, мешок 30 кг', D(2), D(30), D(20), D(100),
        'https://gerkules.com/catalog/smesi-dlya-vyravnivaniya-pola/detail/styazhka-dlya-pola-gf-17/'),
}
ECONOMY_SYSTEM_SOURCE = 'https://gerkules.com/catalog/gidroizolyatsiya/detail/gidroizolyatsiya-gh-106-akva-stop-pro/'
FLOOR_OFF = 'Дно не включено в выбранную схему штукатурки'
ECONOMY_WARNING = ('Проверить систему Геркулес: GP-21/GF-17 предварительно приняты для подготовки '
    'под GH-106 небольших монолитных бассейнов; допустимый размер бассейна не определён. '
    'Система под ПВХ не подтверждена. GF-17 требует прочность основания не менее 20 МПа. '
    'GH-106 в смету не добавлена. Цены и расход контактного слоя уточнить.')


def economy_metrics(inputs):
    result = {}
    dimensions = [inputs.get(k) for k in ('length', 'width', 'bowl_height', 'tech_plaster_thickness')]
    if any(v in (None, '') for v in dimensions) or mode(inputs, 'plaster') not in ('walls', 'walls_floor'):
        return result
    length, width, height, thickness = map(D, dimensions)
    mm = thickness * 1000
    for basis, (_, rate, bag, minimum, maximum, _) in ECONOMY.items():
        if basis == 'plaster_floor_bags' and mode(inputs, 'plaster') != 'walls_floor':
            continue
        area = 2 * (length + width) * height if basis == 'plaster_wall_bags' else length * width
        net = area * mm * rate if minimum <= mm <= maximum else None
        stock = net * D('1.05') if net is not None else None
        result[basis] = {'area': area, 'thickness_mm': mm, 'net': net, 'stock': stock,
                        'bags': (stock / bag).to_integral_value(rounding=ROUND_CEILING) if stock is not None else None}
    return result


def _apply_economy(snapshot, create):
    from .pool_estimates import make_row
    variant = snapshot.get('variants', {}).get('economy')
    if variant is None:
        return []
    all_rows = [r for b in variant['blocks'] for r in b['rows']]
    original = next((r for r in all_rows if r['id'] == IDS[0]), None)
    if _can_initialize(snapshot, 'economy', create) and original is not None and _untouched_placeholder(original):
        original.update(name=ECONOMY['plaster_wall_bags'][0], unit='меш.', basis='plaster_wall_bags', quantity=None)
        block = next(b for b in variant['blocks'] if any(r is original for r in b['rows']))
        insertion = block['rows'].index(original) + 1
        for identity, name, unit, basis in (
            ('tech_plaster_floor', ECONOMY['plaster_floor_bags'][0], 'меш.', 'plaster_floor_bags'),
            (IDS[2], 'Геркулес GM-135 — набрызг по гладкому бетону (уточнить расход)', 'компл.', 'plaster_contact'),
        ):
            if not any(r['id'] == identity for r in all_rows):
                block['rows'].insert(insertion, make_row(identity, name, unit, None, basis))
                insertion += 1
        all_rows = [r for b in variant['blocks'] for r in b['rows']]
    active = [r for r in all_rows if r['id'] in (IDS[0], 'tech_plaster_floor', IDS[2])
              and r.get('basis') in ('plaster_wall_bags', 'plaster_floor_bags', 'plaster_contact')]
    state = mode(snapshot['inputs'], 'plaster')
    if not active:
        return ['Штукатурка: сохранена исходная ручная строка; предварительная система Геркулес не добавлена во избежание двойного начисления.'] if original is not None and state != 'off' else []
    warnings = [ECONOMY_WARNING] if state != 'off' else []
    values = economy_metrics(snapshot['inputs'])
    for row in active:
        basis = row['basis']
        off_reason = OFF if state == 'off' else FLOOR_OFF if state == 'walls' and basis == 'plaster_floor_bags' else None
        if off_reason:
            if not row.get('excluded') or row.get('reason') in (OFF, FLOOR_OFF):
                row.update(excluded=True, reason=off_reason)
            continue
        if row.get('excluded') and row.get('reason') in (OFF, FLOOR_OFF):
            row.update(excluded=False, reason='')
        value = values.get(basis, {})
        if row.get('auto_quantity') and not row.get('existing'):
            quantity = value.get('bags') if row.get('unit') == EXPECTED_UNITS[basis] else None
            row['quantity'] = format(quantity, 'f') if quantity is not None else None
            if row.get('unit') != EXPECTED_UNITS[basis]:
                warnings.append(f"{row['name']}: расчётная единица {EXPECTED_UNITS[basis]}, в строке {row.get('unit')}; автоматическое количество не подтверждено.")
        if basis == 'plaster_contact':
            note = ('По гладкому бетону GP-21 требует набрызг GM-135. Расход и цена не подтверждены; '
                    'количество неизвестно, это не нулевой расход. Источник: ' + ECONOMY['plaster_wall_bags'][5])
        else:
            _, rate, bag, minimum, maximum, source = ECONOMY[basis]
            if value.get('net') is None:
                note = f'Количество не подтверждено: проверьте геометрию и толщину слоя {minimum}–{maximum} мм.'
                warnings.append(row['name'] + ': ' + note)
            else:
                note = (f"Площадь {value['area']} м²; слой {value['thickness_mm']} мм; расход {rate} кг/м²/мм. "
                        f"Без запаса {value['net']} кг; с запасом 5% {value['stock']} кг; "
                        f"закупка {value['bags']} меш. × {bag} кг. ")
            note += ' Источник: ' + source
        row['construction_quantity_note'] = note + ' ' + ECONOMY_WARNING + ' Источник системы: ' + ECONOMY_SYSTEM_SOURCE
    return warnings
