"""Preliminary take-off for private rectangular skimmer pools.

Defaults are visible, editable design assumptions, not universal hydraulic limits.
Material and pool-lining compatibility must be evidenced; unknown is not AISI 316.
"""
from decimal import Decimal, ROUND_UP
import re

PROFILES = {
    'plastic_budget': 'Пластик · эконом (Aquaviva / Aquant)',
    'plastic_europe': 'Пластик · европейские бренды (Kripsol / AstralPool)',
    'steel304': 'Нержавеющая сталь AISI 304',
    'steel316': 'Нержавеющая сталь AISI 316 / 316L',
}
DEFAULTS = {'economy': 'plastic_budget', 'standard': 'plastic_europe', 'premium': 'steel316'}
ROLES = {'skimmer': 'Скиммер', 'inlet': 'Возвратная стеновая форсунка',
         'drain': 'Донный слив', 'vacuum': 'Подключение пылесоса'}
CHANGED = 'Изменён комплект закладных'


def ceil_quantity(value):
    return str(Decimal(value).quantize(Decimal('1'), rounding=ROUND_UP))


def counts(inputs, geometry, variant=None):
    flow = Decimal(geometry['required_flow'])
    if variant:
        pumps = [r for b in variant['blocks'] for r in b['rows'] if r['kind'] == 'pump' and
                 ((not b['excluded'] and not r['excluded']) or r['existing'])]
        if len(pumps) == 1 and Decimal(pumps[0]['quantity']) == 1:
            p = pumps[0]
            nominal = p['specs'].get('flow') or (p.get('catalog') or {}).get('nominal_flow')
            if nominal:
                flow = max(flow, Decimal(nominal))
    result = {
        'skimmer': max(1, int(ceil_quantity(Decimal(geometry['surface']) / Decimal(inputs['skimmer_area']))),
                       int(ceil_quantity(flow / Decimal(inputs['skimmer_flow'])))),
        'inlet': max(2, int(ceil_quantity(flow / Decimal(inputs['inlet_flow'])))),
        'drain': 1 if inputs.get('drain_scheme')=='empty_only' and Decimal(geometry['volume'])<=50 else 2, 'vacuum': 1,
    }
    if variant:
        skimmers = [r for b in variant['blocks'] for r in b['rows']
                    if r['kind'] == 'skimmer' and ((not b['excluded'] and not r['excluded']) or r['existing'])]
        if skimmers and all(r['unit'] in ('шт.', 'компл.') and r['quantity'] is not None and Decimal(r['quantity']) == Decimal(r['quantity']).to_integral_value() for r in skimmers):
            actual = sum((Decimal(r['quantity'] or 0) for r in skimmers), Decimal(0))
            # Preserve the normal hydraulic baseline; scale automatic return
            # points with the number of active skimmers, including manual edits.
            result['inlet'] = max(2, int(ceil_quantity(flow * actual / Decimal(result['skimmer']) / Decimal(inputs['inlet_flow']))))
    return {**{k: str(v) for k, v in result.items()}, 'embedded_work': str(sum(result.values()))}


def ensure_rows(snapshot, only=None):
    from .pool_estimates import make_row, EstimateInputError
    for key, variant in snapshot['variants'].items():
        if only and key != only:
            continue
        profile = variant['embedded_profile']
        block = next(b for b in variant['blocks'] if b['id'] == 'embedded')
        legacy = next((r for r in block['rows'] if r['id'] == 'embedded_supply'), None)
        if legacy and legacy['excluded'] and legacy['reason'] != 'Состав закладных детализирован':
            continue  # a user's excluded legacy supply is not silently reintroduced
        if legacy and not legacy['excluded']:
            if legacy['price'] is not None or legacy['model'] or legacy['existing'] or legacy.get('selection_locked'):
                continue  # priced custom lump sum must not be billed twice
            legacy.update(excluded=True, reason='Состав закладных детализирован')
        q = counts(snapshot['inputs'], snapshot['geometry'], variant)
        for role, label in ROLES.items():
            if any(r.get('embedded_role') == role and (r.get('embedded_profile') == profile or r['existing']) for r in block['rows']):
                continue
            ids = {r['id'] for b in variant['blocks'] for r in b['rows']}
            rid = next((f'{role}_{profile}_{i}' for i in range(100) if f'{role}_{profile}_{i}' not in ids), '')
            if not rid or len(block['rows']) >= 100:
                raise EstimateInputError('В блоке закладных недостаточно места для новых позиций.')
            row = make_row(rid, label + ': подобрать модель', 'шт.', q[role], role, role)
            row.update(embedded_role=role, embedded_profile=profile)
            block['rows'].append(row)


def change_profile(raw, variant_key, profile):
    from .pool_estimates import normalize_snapshot, evaluate, geometry, EstimateInputError
    if not isinstance(profile, str) or profile not in PROFILES or not isinstance(variant_key, str) or variant_key not in DEFAULTS:
        raise EstimateInputError('Выберите комплектацию закладных и КП.')
    result = normalize_snapshot(raw)
    result['geometry'] = geometry(result['inputs'])
    variant = result['variants'][variant_key]
    if variant['embedded_profile'] != profile:
        for block in variant['blocks']:
            if block['id'] != 'embedded':
                continue
            for row in block['rows']:
                if not row.get('embedded_role') or row['existing']:
                    continue
                if row['embedded_profile'] != profile and not row['excluded']:
                    row.update(excluded=True, reason=CHANGED)
                elif row['embedded_profile'] == profile and row['excluded'] and row['reason'] == CHANGED:
                    row.update(excluded=False, reason='')
        variant['embedded_profile'] = profile
    ensure_rows(result, variant_key)
    update_quantities(result, variant_key)
    return evaluate(result)


def update_quantities(snapshot, only=None):
    for key, variant in snapshot['variants'].items():
        if only and key != only:
            continue
        q = counts(snapshot['inputs'], snapshot['geometry'], variant)
        for block in variant['blocks']:
            for row in block['rows']:
                if row['auto_quantity'] and not row['existing'] and row['basis'] in q:
                    row['quantity'] = format(Decimal(q[row['basis']]), '.3f')


def specifications(name, description, brand):
    title = name.casefold().replace('ё', 'е')
    from html import unescape
    description = unescape(re.sub(r'<[^>]*>', ' ', description))
    text = (title + ' ' + description.casefold()).replace('ё', 'е')
    role = ''
    if re.match(r'^скиммер\b', title) and not re.search(r'навесн|intex|bestway|комплект|крышк[аи] для', title):
        role = 'skimmer'
    elif re.match(r'^(?:донный слив|слив донный)\b', title):
        role = 'drain'
    elif re.match(r'^форсунка\b', title):
        if re.search(r'пылесос', title):
            role = 'vacuum'
        elif not re.search(r'гидромасс|донн|перелив|всасыва|воздуш|противоток|spa|спа|фонтан', title):
            role = 'inlet'
    from .estimate_embedded_candidates import body_grade_claims, lining_claims, title_conflict, without_hardware_grades
    title_body = without_hardware_grades(title)
    material = ''
    # Titles with both grades (304/316) do not establish either grade.
    grade_labels = re.findall(r'\baisi[ -]*(?:304|316)(?:l|л)?(?:\s*/\s*(?:304|316)(?:l|л)?)?\b', title_body, re.I)
    grades = set(re.findall(r'304|316', ' '.join(grade_labels)))
    # A grade attached to hardware does not establish the body material. Mixed or
    # contradictory descriptions require manual verification, never a 316 guess.
    body_uncertain = re.search(r'пласт|\babs\b|абс|крышк|винт|болт|вставк|решетк|флан|рамк|накладк', title)
    body_plastic = re.search(r'корпус[^.;]{0,40}(?:\babs\b|абс|пласт)|(?:\babs\b|абс|пласт)[^.;]{0,30}корпус', text)
    if len(grades) == 1 and not body_uncertain and not body_plastic:
        material = 'steel' + next(iter(grades))
    elif not grades and re.search(r'\babs\b|абс[ -]|пластик', text) and not re.search(r'нерж|бронз|aisi', title_body):
        material = 'plastic'
    # Explicitly documented ABS families; not a brand-wide assumption.
    # Kripsol residential catalogue 2021, pp. 46-48 (see docs/ai/pool-estimates.md).
    if not material and not grades and brand.casefold() == 'kripsol' and re.search(r'\b(?:sksl?|skal?|srpl?|bor\s*20|bol\s*20|bof\s*21|bll|blrr|blre)\b', title):
        material = 'plastic'
    positive_lining, _ = lining_claims(title)
    _, lining_denied = lining_claims(text)
    lining = [kind for kind in ('film', 'concrete') if kind in positive_lining]
    conflict_note = title_conflict(title)
    body_grades = body_grade_claims(text)
    if body_grades:
        if len(body_grades) != 1 or body_grades - {'304', '316'}:
            conflict_note = 'Явно указанная марка стали корпуса не входит в единственное подтверждённое исполнение AISI 304/316: ' + '/'.join(sorted(body_grades)) + '.'
        else:
            body_material = 'steel' + next(iter(body_grades))
            if material and material != body_material:
                conflict_note = 'Противоречивые указания марки/материала корпуса в названии и описании.'
            else:
                material = body_material
    if positive_lining & lining_denied:
        conflict_note = 'Противоречивые указания отделки в названии/описании.'
    # Explicit contradictory body claims are not missing data. Hardware can be
    # another material; only an explicit body claim participates here.
    body_metal = re.search(r'корпус\s*[:—-]?\s*(?:из\s+)?(?:нержав\w*|сталь|стали|бронз\w*|латун\w*)', text)
    title_plastic = re.search(r'\babs\b|абс|пластик', title)
    hardware = re.search(r'винт|болт|вставк|решетк|флан|рамк|накладк|крышк', title)
    if (body_plastic and body_metal) or (grades and body_plastic and not body_uncertain) or (grades and title_plastic and not hardware):
        conflict_note = 'Противоречивые указания материала корпуса в названии/описании.'
    body_patterns = {
        'plastic': r'(?:abs\b|абс\b|пласт\w*)',
        'steel': r'(?:нержав\w*|сталь\w*|стали|aisi\s*[- ]?\s*\d{3})',
        'other_metal': r'(?:бронз\w*|латун\w*|алюмин\w*|титан\w*|мед[ьи]\w*)',
    }
    body_prefix = r'(?:материал\s+корпуса|корпус)\s*[:—-]?\s*(?:(?:выполнен|изготовлен)\s+)?(?:из\s+)?'
    body_families = {family for family, pattern in body_patterns.items()
                     if re.search(body_prefix + pattern, text)
                     or re.search(pattern + r'\s+корпус\b', text)}
    if material:
        body_families.add('plastic' if material == 'plastic' else 'steel')
    body_family = next(iter(body_families)) if len(body_families) == 1 else ''
    if len(body_families) > 1:
        conflict_note = 'Противоречивые указания материала корпуса в названии/описании.'
    # SRP/SRPL concrete/liner designations are explicit in the same catalogue.
    if role == 'drain' and brand.casefold() == 'kripsol' and not lining:
        if re.search(r'\bsrp\b', title):
            lining = ['concrete']
        elif re.search(r'\bsrpl\b', title):
            lining = ['film']
    result = {'embedded_role': role, 'material': material, 'lining': lining,
              'body_family': body_family, 'lining_denied': sorted(lining_denied)}
    if role and conflict_note:
        result.update(material='', lining=[], specification_conflict=True, specification_note=conflict_note)
    return result


def matches(product, profile, finish):
    if not product['available'] or not product.get('embedded_role'):
        return False
    lining = 'film' if finish == 'film' else 'concrete' if finish in ('tile', 'mosaic') else ''
    if not lining or lining not in product.get('lining', []):
        return False
    return profile_matches(product, profile)


def profile_matches(product, profile):
    material = product.get('material')
    if profile == 'plastic_budget':
        return material == 'plastic' and product['brand'].casefold() in {'aquaviva', 'aquant'}
    if profile == 'plastic_europe':
        return material == 'plastic' and product['brand'].casefold() in {'kripsol', 'astralpool', 'astral'}
    return material == profile
