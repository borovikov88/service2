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


def warnings(variant):
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
    return [prefix + text + ' Форсунка регулятора не учитывается как возвратная форсунка фильтрации.']


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
        specs.update(kind='level_regulator', embedded_role='')
