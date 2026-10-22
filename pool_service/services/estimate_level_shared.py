"""One additional physical fitting of a filtration model, with separate duty count."""
from decimal import Decimal, InvalidOperation


def normalize_row_split(item, raw):
    from .pool_estimates import EstimateInputError, number
    if item['kind'] != 'inlet':
        return
    keys = ('filtration_quantity', 'level_quantity')
    if not any(k in raw for k in keys):
        return
    try:
        filtration, level = (number(raw[k], 'Количество форсунок', maximum='1000000', places=None) for k in keys)
        total = Decimal(item['quantity'])
        if not all(n.is_finite() for n in (filtration, level, total)) or filtration < 0 or level not in (0, 1):
            raise ValueError
    except (KeyError, TypeError, InvalidOperation, ValueError):
        raise EstimateInputError('Некорректное разделение количества форсунок фильтрации и уровня.')
    # A normal quantity edit carries the previous split. Respect the new total;
    # never silently add another item to a manual quantity, including zero.
    credit=number(raw.get('level_quantity_credit','0'),'Резерв форсунки уровня',maximum='1',places=None)
    if credit not in (0,1): raise EstimateInputError('Некорректный резерв форсунки уровня.')
    if filtration + level + credit != total:
        credit=Decimal(0)
        level = level if total >= level else Decimal(0)
        filtration = total - level
    item.update(filtration_quantity=f'{filtration:.3f}',level_quantity=f'{level:.3f}',level_quantity_credit=str(credit if total>=credit else 0))


def filtration_quantity(row):
    return Decimal(row.get('filtration_quantity', row['quantity']) if row['kind'] == 'inlet' else row['quantity'])


def active(variant, kind):
    return [r for b in variant['blocks'] for r in b['rows']
            if r['kind'] == kind
            and ((not b['excluded'] and not r['excluded']) or (r['existing'] and kind != 'inlet'))
            and (kind != 'inlet' or not r['existing'])
            and r['quantity'] is not None and Decimal(r['quantity']) > 0]


def linked(variant, automatic=False):
    rows = [r for r in active(variant, 'inlet') if r['unit'] == 'шт.']
    if automatic:
        row=next((r for r in rows if r['id']==variant.get('level_inlet_row_id')),None) or next(iter(sorted(rows,key=lambda r:r['id'])),None)
        variant['level_inlet_row_id']=row['id'] if row else ''
        variant['level_inlet_link_manual']=False
        return row
    if variant.get('level_inlet_link_manual'):
        return next((r for r in rows if r['id'] == variant.get('level_inlet_row_id')), None)
    if len(rows) == 1:
        variant['level_inlet_row_id'] = rows[0]['id']
        return rows[0]
    return None


def evidence(row):
    return dict(row.get('catalog') or {}, kind='inlet', embedded_role='inlet', available=True,
                name=row.get('model') or row['name'], brand=row.get('brand', ''))


def blocked(row, regulator, variant, inputs):
    from .estimate_embedded_candidates import assess
    from .estimate_level_control import regulator_reason
    product = evidence(row)
    reason, _ = assess(product, variant['embedded_profile'], inputs['finish'])
    if reason:
        return reason
    if regulator and regulator_reason(regulator):
        return regulator_reason(regulator)
    if any(p.get('level_purpose') == 'filtration' or p.get('level_passage') == 'no' for p in (product, regulator or {})):
        return 'Подтверждено неподходящее назначение или отсутствие свободного прохода для линии уровня.'
    if regulator:
        for key in ('level_diameter', 'level_connection'):
            if product.get(key) and regulator.get(key) and product[key] != regulator[key]:
                return 'Подтверждено несовпадение соединений форсунки и регулятора уровня.'
    return None


def sync(snapshot):
    from .estimate_embedded import counts
    from .estimate_level_control import inlet_reason
    for variant in snapshot['variants'].values():
        variant['level_shared_inlet'] = 1
        notes = []
        automatic=snapshot.get('level_shared_quantity_version')==2
        row = linked(variant, automatic)
        regs = active(variant, 'level_regulator')
        notes.extend('Регулятор уровня: ' + reg['catalog_review_note'] for reg in regs if reg.get('catalog_review_note'))
        included = variant.get('level_control_status') == 'included'
        wanted = not included and len(regs) == 1 and row is not None
        if not included and not row:
            notes.append('Проверить: выберите строку модели форсунки для линии уровня; при нескольких моделях связь не определяется автоматически.')
        if not included and len(regs) != 1:
            notes.append('Проверить: для линии уровня нужен один активный внешний регулятор.')
        if active(variant, 'level_inlet'):
            wanted = False
            notes.append('Проверить: сохранена отдельная строка форсунки уровня; дополнительная единица в строку фильтрации не добавлена во избежание дублирования.')
        if wanted:
            regulator = regs[0].get('catalog') or {}
            issue = regs[0].get('catalog_review_note') or blocked(row, regulator, variant, snapshot['inputs'])
            if issue:
                if not automatic: wanted = False
                notes.append('Проверить линию уровня: ' + issue + (' Дополнительная форсунка учтена только в количестве; инженерная совместимость не подтверждена.' if automatic else ' Выбранная модель сохранена; дополнительная единица автоматически не добавлена.'))
            else:
                reason = inlet_reason(evidence(row), regulator, snapshot['inputs'], variant['embedded_profile'])
                if reason:
                    notes.append('Проверить совместимость модели форсунки с линией уровня: ' + reason)
        q = counts(snapshot['inputs'], snapshot['geometry'], variant)
        for b in variant['blocks']:
            for item in b['rows']:
                if item['kind'] != 'inlet':
                    continue
                old_level = Decimal(item.get('level_quantity', '0'))
                total = Decimal(item['quantity'] or '0')
                desired = Decimal(1 if wanted and item is row else 0)
                if item['auto_quantity'] and not item['existing']:
                    base = Decimal(q['inlet']) if item['basis'] == 'inlet' else max(Decimal(0), total - old_level)
                    item.update(filtration_quantity=f'{base:.3f}', level_quantity=f'{desired:.3f}', quantity=f'{base + desired:.3f}',level_quantity_credit='0')
                else:
                    if automatic and not item['existing'] and item['unit']=='шт.':
                        credit=Decimal(item.get('level_quantity_credit','0'))
                        if desired>old_level and total>0:
                            needed=desired-old_level
                            total+=max(Decimal(0),needed-credit)
                            credit=max(Decimal(0),credit-needed)
                        elif desired<old_level:
                            credit=min(Decimal(1),credit+old_level-desired)
                        item['level_quantity_credit']=str(credit if total>=credit else 0)
                        item['quantity']=f'{total:.3f}'
                        old_level=desired
                    # Preserve the user's physical total, not a reservation for
                    # a line that no longer exists (builtin/unlink/rebinding).
                    level = old_level if desired and total >= old_level else Decimal(0)
                    credit=Decimal(item.get('level_quantity_credit','0'))
                    item.update(filtration_quantity=f'{max(Decimal(0),total-level-credit):.3f}', level_quantity=f'{level:.3f}')
                    if level != desired:
                        notes.append('Проверить ручное количество форсунок: дополнительная единица для уровня не добавляется и не удаляется автоматически; укажите количество с учётом линии уровня.')
                item['level_quantity_note'] = (
                    f"Фильтрация: {Decimal(item['filtration_quantity']).normalize():f} шт.; линия уровня: {Decimal(item['level_quantity']).normalize():f} шт."
                )
                if Decimal(item.get('level_quantity_credit','0')):
                    item['level_quantity_note']+=' Резерв линии уровня: 1 шт.; не участвует в фильтрации.'
        if included:
            notes.append('Регулятор уровня подтверждён в комплекте скиммера; автоматическая дополнительная форсунка не требуется.')
            if regs:
                notes.append('Конфликт комплектации: ручной внешний регулятор сохранён; проверьте дублирование.')
        if variant.get('level_control_status') == 'unknown':
            notes.append('Проверить: наличие регулятора уровня в комплекте скиммера не подтверждено.')
        variant['level_shared_warnings'] = notes


def fill(snapshot, catalog, only=None):
    from .estimate_level_control import editable, regulator_reason, inlet_matches, ROLES
    from .estimate_catalog import apply_product, refresh_auto_facts, select_price_tier
    filled = 0
    for tier, variant in snapshot['variants'].items():
        if only and tier != only:
            continue
        regs, row = active(variant, 'level_regulator'), linked(variant, snapshot.get('level_shared_quantity_version')==2)

        def rejection(product):
            if product.get('kind', 'level_regulator') != 'level_regulator':
                return 'Назначение товара как регулятора уровня не подтверждено.'
            return regulator_reason(product) or (blocked(row, product, variant, snapshot['inputs']) if row else None)

        # Protected rows keep their frozen facts and money. Persist the explicit
        # catalogue review so evaluate/save/recalculate cannot erase the warning.
        for reg in regs:
            origin = reg.get('catalog')
            current = catalog['products'].get(origin['id']) if origin else None
            if current:
                issue = rejection(current)
                reg['catalog_review_note'] = ('Текущая выгрузка: ' + issue)[:1000] if issue else ''
        if variant.get('level_control_status') == 'included' or len(regs) != 1 or not editable(regs[0]):
            continue
        reg = regs[0]
        origin = reg.get('catalog')
        choices = [p for p in catalog['products'].values()
                   if p['kind'] == 'level_regulator' and not rejection(p)]
        confirmed = [p for p in choices if row and inlet_matches(evidence(row), p, snapshot['inputs'], variant['embedded_profile'])]
        chosen = select_price_tier(confirmed or choices, tier)
        if chosen:
            if origin and origin['id'] == chosen['id']:
                refresh_auto_facts(reg, chosen, catalog)
            else:
                apply_product(reg, chosen, catalog, 'auto')
                filled += 1
        elif origin:
            current = catalog['products'].get(origin['id']) or origin
            issue = rejection(current)
            if issue:
                reg.update(name=ROLES['level_regulator'], brand='', model='', price=None, catalog=None,
                           catalog_review_note='', selection_note='Проверить: прежний автоматический кандидат отклонён. ' + issue + ' Подходящая замена не найдена; цена не включена в итог.')
                reg['specs'] = dict.fromkeys(('flow', 'head', 'filter_flow', 'backwash_flow'))
                reg.pop('selection_status', None)
    sync(snapshot)
    return filled


def apply_binding(raw, tier, row_id):
    from .pool_estimates import evaluate, EstimateInputError
    snapshot = evaluate(raw)
    if snapshot.get('level_shared_inlet') != 1 or tier not in snapshot['variants']:
        raise EstimateInputError('Связь форсунки уровня доступна только в новом расчёте.')
    if snapshot.get('level_shared_quantity_version')==2:
        raise EstimateInputError('В текущей схеме форсунка уровня назначается автоматически.')
    variant = snapshot['variants'][tier]
    if row_id and not any(r['id'] == row_id and r['unit'] == 'шт.' for r in active(variant, 'inlet')):
        raise EstimateInputError('Выберите активную строку форсунки в штуках.')
    variant.update(level_inlet_row_id=row_id, level_inlet_link_manual=True)
    return evaluate(snapshot)
