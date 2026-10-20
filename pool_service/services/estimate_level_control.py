"""One level-control system per bowl; never counted as filtration returns."""
from decimal import Decimal

ROLES = {'level_regulator':'Регулятор уровня воды: подобрать модель',
         'level_inlet':'Дополнительная форсунка регулятора уровня: подобрать модель'}
OFF = 'Регулятор уровня включён в комплект выбранного скиммера'
STATES = {'unknown', 'included', 'absent'}


def sync(snapshot, create=False):
    if not snapshot.get('level_system'):
        return
    from .pool_estimates import make_row, EstimateInputError
    for variant in snapshot['variants'].values():
        block = next(b for b in variant['blocks'] if b['id']=='embedded')
        rows = [r for b in variant['blocks'] for r in b['rows']]
        skimmers = [r for b in variant['blocks'] for r in b['rows'] if r['kind']=='skimmer' and
                    ((not b['excluded'] and not r['excluded']) or r['existing']) and r['quantity'] is not None and Decimal(r['quantity']) > 0]
        included = any(status(r) == 'included' for r in skimmers)
        for kind, title in ROLES.items():
            candidates = [r for r in rows if r['kind']==kind]
            legacy_bundle = any(r['id'] == 'embedded_supply' and not (r['excluded'] and r['reason'] == 'Состав закладных детализирован') for r in rows)
            if not candidates and create and not legacy_bundle:
                if len(block['rows']) >= 100:
                    raise EstimateInputError('В блоке недостаточно места для системы уровня воды.')
                ids = {r['id'] for r in rows}
                rid = kind if kind not in ids else next((f'{kind}_{i}' for i in range(100) if f'{kind}_{i}' not in ids),'')
                if not rid: raise EstimateInputError('Не удалось добавить систему уровня воды.')
                row=make_row(rid,title,'шт.','1','level_system',kind)
                row['level_managed']=True
                block['rows'].append(row);rows.append(row);candidates=[row]
            for row in candidates:
                if not row.get('level_managed') or row['existing'] or row.get('selection_locked') or not row['auto_quantity'] or (row.get('catalog') or {}).get('selection') == 'manual':
                    continue
                if included and not row['excluded']:
                    row.update(excluded=True,reason=OFF)
                elif not included and row['excluded'] and row['reason']==OFF:
                    row.update(excluded=False,reason='')
                if row['auto_quantity'] and row['basis']=='level_system':
                    row['quantity']='1.000'
        variant['level_control_status'] = 'included' if included else 'absent' if skimmers and all(status(r)=='absent' for r in skimmers) else 'unknown'


def warnings(variant, inputs=None):
    state = variant.get('level_control_status')
    if not state: return []
    rows = [r for b in variant['blocks'] for r in b['rows'] if r['kind'] in ROLES and
            ((not b['excluded'] and not r['excluded']) or r['existing']) and
            r['quantity'] is not None and Decimal(r['quantity']) > 0]
    roles = {r['kind'] for r in rows}
    if state == 'included':
        text = 'В выбранном скиммере подтверждён регулятор уровня в комплекте. '
        text += ('Конфликт комплектации: отдельные ручные/установленные позиции системы уровня сохранены. Проверьте дублирование и явно исключите лишнее.' if rows else
                 'Активных отдельных позиций системы уровня нет; проверьте, что встроенный комплект обслуживает всю чашу.')
        return [text]
    prefix = 'Комплектация скиммера регулятором уровня не подтверждена. ' if state == 'unknown' else ''
    text = ('В смете есть отдельный регулятор и его форсунка; проверьте состав и количество для одной чаши.' if roles == set(ROLES) else
            'Отдельная система уровня не сформирована полностью: проверьте отсутствующие или исключённые позиции и прежний общий комплект.')
    notes = [prefix + text + ' Форсунка регулятора не учитывается как возвратная форсунка фильтрации.']
    regs = [r for r in rows if r['kind'] == 'level_regulator']
    inlets = [r for r in rows if r['kind'] == 'level_inlet']
    for reg in regs:
        product = reg.get('catalog') or {}
        if product.get('specification_conflict'):
            notes.append('Регулятор ' + reg['name'] + ': конфликт характеристик. Ручная позиция сохранена; автоматическая совместимость форсунки не подтверждена.')
        if product.get('level_scheme') == 'external_float':
            notes.append('Регулятор ' + reg['name'] + ': предварительный подбор. Проверьте давление водоснабжения и монтаж по паспорту поставляемой модели; отделка чаши относится к форсунке, а не к внешнему баку.')
            verified = inputs and any(inlet_matches(
                dict(r.get('catalog') or {}, kind='inlet', available=True, brand=r.get('brand', '')),
                product, inputs, variant['embedded_profile']) for r in inlets)
            if not verified:
                notes.append('Для регулятора арт. ' + product.get('sku', '') + ' не подтверждена отдельная форсунка. Укажите в её характеристиках совместимый артикул регулятора, материал корпуса и отделку. Ручные позиции сохранены для проверки.')
    return notes


def status(row):
    # Explicit user evidence belongs to this row/model; never inferred from metal.
    if row.get('level_evidence') and row.get('level_status') in STATES:
        return row['level_status']
    return (row.get('catalog') or {}).get('level_control', 'unknown')


def classify(specs, name, brand, sku):
    import re
    models = {'16791': ('aquaviva', r'RO[- ]?7'), '528': ('kripsol', r'RGP[.]?C'),
              '32304': ('hayward', r'3150')}
    expected = models.get(sku)
    if (expected and brand.casefold() == expected[0] and
            re.search(r'(?<![a-z0-9])' + expected[1] + r'(?![a-z0-9])', name, re.I) and
            re.search(r'регулятор.*уров', name, re.I) and not re.search(r'рамк|датчик', name, re.I)):
        specs.update(kind='level_regulator', embedded_role='', level_scheme='external_float')
        # Only the external float-tank scheme is established here. Do not transfer
        # Emaux pressure limits to an Aquaviva-labelled SKU, infer fittings, or
        # impose the bowl lining/steel grade on a tank outside the bowl.
        specs['specification_note'] = (
            'Предварительный подбор внешнего поплавкового регулятора. '
            'Схема: отдельное подключение к чаше; совместимая форсунка проверяется отдельно. '
            'Давление водоснабжения, обвязку и условия монтажа проверить по паспорту поставляемой модели.')


def regulator_reason(product):
    if not product.get('available', True):
        return 'Выгрузка магазина не подтверждает доступность к заказу.'
    if product.get('specification_conflict'):
        return 'Конфликт характеристик регулятора; проверьте XML param.'
    if product.get('level_scheme') != 'external_float':
        return ('Не подтверждена схема этой модели: внешний поплавковый регулятор '
                'с отдельным подключением к чаше. Укажите схему в характеристиках товара.')
    return None


def inlet_matches(product, regulator, inputs, profile):
    from . import estimate_embedded as embedded
    return (not regulator_reason(regulator) and product.get('kind') in {'inlet', 'vacuum'} and not product.get('specification_conflict')
            and regulator.get('sku') in product.get('level_regulator_skus', [])
            and embedded.matches(product, profile, inputs['finish']))


def editable(row):
    if (not row.get('level_managed') or row['excluded'] or row['existing'] or row.get('selection_locked')
            or not row['auto_quantity'] or Decimal(row['quantity']) != 1 or row.get('unit_price_manual')):
        return False
    origin = row.get('catalog')
    if origin:
        return origin['selection'] == 'auto' and row['price'] == origin['price']
    return row['price'] is None and not row['model'] and row['name'] == ROLES[row['kind']]


def fill(snapshot, catalog, only=None):
    """Separate external tank from finish-specific wall fitting. Never guess a pair.

    Complete verified pairs are preferred before price ranking. A known external
    tank may be proposed alone, with an explicit missing-fitting warning. This is
    a preliminary BOM, not approval of supply pressure/installation on the site.
    """
    from .estimate_catalog import apply_product, select_price_tier
    sync(snapshot)
    filled = 0
    products = list(catalog['products'].values())
    for tier, variant in snapshot['variants'].items():
        if only and only != tier:
            continue
        block = next(b for b in variant['blocks'] if b['id'] == 'embedded')
        if block['excluded'] or variant.get('level_control_status') == 'included':
            continue
        rows = [r for r in block['rows'] if r['kind'] in ROLES and not r['excluded']]
        regs = [r for r in rows if r['kind'] == 'level_regulator']
        inlets = [r for r in rows if r['kind'] == 'level_inlet']
        if len(regs) != 1 or len(inlets) != 1:
            continue  # multiple/manual systems need an explicit connection, not a guess
        reg, inlet = regs[0], inlets[0]
        def compatible(p):
            return [i for i in products if inlet_matches(i, p, snapshot['inputs'], variant['embedded_profile'])]
        if editable(reg):
            choices = [p for p in products if p['kind'] == 'level_regulator' and not regulator_reason(p)]
            if not editable(inlet):
                # Preserve manual choices and use their frozen compatibility evidence.
                evidence = dict(inlet.get('catalog') or {}, kind='inlet', available=True,
                                brand=inlet.get('brand', ''))
                choices = [p for p in choices if inlet_matches(evidence, p, snapshot['inputs'], variant['embedded_profile'])]
            paired = [p for p in choices if compatible(p)]
            product = select_price_tier(paired or choices, tier)
            if product and (reg.get('catalog') or {}).get('id') != product['id']:
                apply_product(reg, product, catalog, 'auto')
                filled += 1
        chosen = reg.get('catalog')
        if chosen and not regulator_reason(chosen) and editable(inlet):
            product = select_price_tier(compatible(chosen), tier)
            if product and (inlet.get('catalog') or {}).get('id') != product['id']:
                apply_product(inlet, product, catalog, 'auto')
                filled += 1
    return filled
