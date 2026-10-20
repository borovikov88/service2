"""One level-control system per bowl; never counted as filtration returns."""
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
                    ((not b['excluded'] and not r['excluded']) or r['existing']) and r['quantity'] is not None and float(r['quantity']) > 0]
        included = any((r.get('catalog') or {}).get('level_control','unknown') == 'included' for r in skimmers)
        for kind, title in ROLES.items():
            candidates = [r for r in rows if r['kind']==kind]
            if not candidates and create:
                if len(block['rows']) >= 100:
                    raise EstimateInputError('В блоке недостаточно места для системы уровня воды.')
                ids = {r['id'] for r in rows}
                rid = kind if kind not in ids else next((f'{kind}_{i}' for i in range(100) if f'{kind}_{i}' not in ids),'')
                if not rid: raise EstimateInputError('Не удалось добавить систему уровня воды.')
                row=make_row(rid,title,'шт.','1','level_system',kind)
                row['level_managed']=True
                block['rows'].append(row);rows.append(row);candidates=[row]
            for row in candidates:
                if not row.get('level_managed') or row['existing']:
                    continue
                if included and not row['excluded']:
                    row.update(excluded=True,reason=OFF)
                elif not included and row['excluded'] and row['reason']==OFF:
                    row.update(excluded=False,reason='')
                if row['auto_quantity'] and row['basis']=='level_system':
                    row['quantity']='1.000'
        variant['level_control_status'] = 'included' if included else 'absent' if skimmers and all((r.get('catalog') or {}).get('level_control')=='absent' for r in skimmers) else 'unknown'


def warnings(variant):
    state=variant.get('level_control_status')
    if state == 'unknown':
        return ['Комплектация скиммера регулятором уровня не подтверждена. Предварительно добавлена одна отдельная система на чашу; уточните точную модель и комплект. Дополнительная форсунка регулятора не учитывается как возвратная форсунка фильтрации.']
    if state == 'included':
        return ['В выбранном скиммере подтверждён регулятор уровня в комплекте; автоматически добавленная отдельная пара исключена. Проверьте, что он обслуживает общую чашу. Ручные и уже установленные позиции требуют отдельной проверки на дублирование.']
    return ['Отдельный регулятор уровня и его форсунка: одна система на чашу, без включения этой форсунки в расчёт фильтрации.'] if state else []
