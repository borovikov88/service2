"""Preliminary wall-fitting assessment; never convert unknowns into facts.

Inputs are frozen catalogue facts plus the original product title. The caller
ranks eligible candidates by missing-data count, then its usual price tier.
"""
import re


def lining_claims(text):
    text = (text or '').casefold().replace('ё', 'е')
    patterns = {'film': r'(?:пленк\w*|лайнер\w*)',
                'concrete': r'(?:бетон\w*|плитк\w*|мозаик\w*)'}
    item = '(?:' + '|'.join(patterns.values()) + ')'
    # Consume only an explicit coordinated list of finish words. In particular,
    # never extend a negation through a full stop or an independent sentence.
    separator = r'(?:\s*(?:,|/|\bи\b|\bили\b|\bлибо\b|\bа также\b)\s*(?:и\s+)?)'
    negative = (r'\bне\s+(?:(?:подходит|предназначен\w*)\s+)?(?:для|под)\s+'
                + item + '(?:' + separator + item + ')*')
    denied = set()
    def remove_negative(match):
        denied.update(kind for kind, pattern in patterns.items() if re.search(pattern, match.group()))
        return ' '
    text = re.sub(negative, remove_negative, text)
    positive = {kind for kind, pattern in patterns.items() if re.search(pattern, text)}
    return positive, denied


def body_grade_claims(text):
    """Only a grade attached directly to BODY material, never nearby fasteners."""
    prefix = r'(?:материал\s+корпуса|корпус)\s*[:—-]?\s*(?:(?:выполнен|изготовлен)\s+)?(?:из\s+)?'
    steel = r'(?:(?:нержавеющ\w*\s+)?стал[ьи]\s+)?'
    grade = r'aisi[ -]*\d{3}(?:l|л)?(?:\s*/\s*\d{3}(?:l|л)?)*\b'
    claims = re.findall(prefix + steel + grade, text, re.I)
    return set(re.findall(r'\d{3}', ' '.join(claims)))


def without_hardware_grades(name):
    # Only an explicitly attached hardware grade is excluded. A body grade
    # elsewhere in the same title remains evidence and can reject the profile.
    return re.sub(r'\b(?:винт|болт|крепеж|накладк|решетк|фланц|рамк|крышк)\w*\s+(?:из\s+)?(?:нержавеющ\w*\s+)?(?:стал[ьи]\s+)?aisi[ -]*\d{3}(?:l|л)?(?:\s*/\s*\d{3}(?:l|л)?)*\b',
                  '', name or '', flags=re.I)


def title_conflict(name):
    title = (name or '').casefold().replace('ё', 'е')
    identity = without_hardware_grades(title)
    all_labels = re.findall(r'\baisi[ -]*\d{3}(?:l|л)?(?:\s*/\s*\d{3}(?:l|л)?)*\b', identity)
    codes = set(re.findall(r'\d{3}', ' '.join(all_labels)))
    if codes - {'304', '316'}:
        return 'Указанная марка стали не входит в подтверждённые исполнения AISI 304/316; требуется отдельная проверка корпуса.'
    labels = re.findall(r'\baisi[ -]*(?:304|316)(?:l|л)?(?:\s*/\s*(?:304|316)(?:l|л)?)?\b', identity)
    if len(set(re.findall(r'304|316', ' '.join(labels)))) > 1:
        return 'В названии указаны разные марки стали AISI 304/316; исполнение корпуса не определено.'
    positive, denied = lining_claims(title)
    if positive & denied:
        return 'Название содержит противоречивые указания отделки бассейна.'
    return None


def assess(product, profile, finish):
    """Return (hard rejection or None, missing evidence); never mutate product."""
    from .estimate_embedded import PROFILES, ROLES, profile_matches, specifications
    if profile not in PROFILES:
        return 'Не выбран допустимый профиль комплектации закладных.', []
    if not product.get('available'):
        return 'Доступность товара к заказу не подтверждена.', []
    if not product.get('embedded_role') or product['embedded_role'] not in ROLES:
        return 'Назначение товара как нужной закладной не подтверждено.', []
    if product.get('specification_conflict') or product.get('level_data_issue') == 'conflict':
        return 'Конфликт характеристик закладной. ' + product.get('specification_note', ''), []
    if product.get('inlet_taxonomy',{}).get('issue'):
        return product['inlet_taxonomy']['issue'], []
    issue = title_conflict(product.get('name', ''))
    if issue:
        return issue, []
    finish_kind = 'film' if finish == 'film' else 'concrete' if finish in ('tile', 'mosaic') else ''
    _, denied = lining_claims(product.get('name', ''))
    denied |= set(product.get('lining_denied') or [])
    if finish_kind and finish_kind in denied:
        return 'В названии/описании явно запрещено исполнение для выбранной отделки бассейна.', []
    title_facts = specifications(without_hardware_grades(product.get('name', '')), '', product.get('brand', ''))
    if title_facts.get('specification_conflict'):
        return title_facts['specification_note'], []
    family = product.get('body_family') or title_facts.get('body_family')
    expected_family = 'plastic' if profile.startswith('plastic_') else 'steel'
    if family and family != expected_family:
        return 'Известный материал корпуса не соответствует выбранному профилю КП.', []
    missing = []
    material = product.get('material', '')
    brand = product.get('brand', '').strip().casefold()
    if material:
        expected = 'plastic' if profile.startswith('plastic_') else profile
        if material != expected:
            return 'Материал корпуса не соответствует выбранному профилю КП.', []
    else:
        title_material = title_facts.get('material')
        expected = 'plastic' if profile.startswith('plastic_') else profile
        if title_material and title_material != expected:
            return 'Материал, явно указанный в названии, не соответствует профилю КП.', []
        # A stated metal body is not an unknown plastic body, even if grade is absent.
        title = product.get('name', '').casefold().replace('ё', 'е')
        metal_body = re.search(r'корпус\s*[:—-]?\s*(?:из\s+)?(?:нержав\w*|сталь|стали|бронз\w*|латун\w*)', title)
        plastic_body = re.search(r'корпус\s*[:—-]?\s*(?:из\s+)?(?:abs|абс|пласт\w*)', title)
        if (profile.startswith('plastic_') and metal_body) or (profile.startswith('steel') and plastic_body):
            return 'Явно указанный материал корпуса не соответствует профилю КП.', []
        missing.append('материал корпуса' if profile.startswith('plastic_') else 'марка стали корпуса ' + profile.replace('steel', 'AISI '))
    if profile.startswith('plastic_'):
        allowed = {'aquaviva', 'aquant'} if profile == 'plastic_budget' else {'kripsol', 'astralpool', 'astral'}
        if brand and brand not in allowed:
            return 'Бренд не соответствует выбранному профилю КП.', []
        if not brand:
            missing.append('бренд для выбранного профиля КП')
    lining = product.get('lining') or []
    if finish_kind and lining and finish_kind not in lining:
        return 'Известное исполнение закладной не соответствует отделке чаши.', []
    if finish_kind and not lining and title_facts['lining'] and finish_kind not in title_facts['lining']:
        return 'Отделка, явно указанная в названии, не соответствует отделке чаши.', []
    if not finish_kind:
        missing.append('тип отделки чаши')
    if not lining:
        missing.append('исполнение закладной под отделку чаши')
    # Full evidence agrees with the established strict matcher. A missing fact
    # remains absent; callers must not label this result engineering approval.
    if material and (brand or not profile.startswith('plastic_')) and not profile_matches(product, profile):
        return 'Характеристики не соответствуют выбранному профилю КП.', []
    return None, missing
