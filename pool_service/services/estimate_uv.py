"""Optional UV equipment: one manual-choice placeholder, no automatic model."""
import re

from .estimate_catalog_params import text

TITLE = 'УФ-установка: выбрать модель'
OFF = 'УФ-установка отключена'
CHECK = 'Выбрано вручную; автоматическая проверка соответствия бассейну пока не выполняется.'
UV_WORD = r'(?:ультрафиолетов\w*|уф|uv(?:[- ]?c)?)'
UNIT = r'(?:установк\w*|стерилизатор\w*|обеззараживател\w*|реактор\w*)'


def classify(specs, name, category):
    """A complete-unit title or UV category plus unit title, never UV mentions."""
    title, path = text(name), text(category)
    if specs['kind'] != 'other' or re.search(r'запчаст|комплектующ|запасн', path + ' ' + title):
        return
    direct = re.match(r'^' + UV_WORD + r'\s*[-–—]?\s*(?:фотокаталитическ\w*\s+)?' + UNIT + r'\b', title)
    reverse = re.match(r'^' + UNIT + r'\s+' + UV_WORD + r'\b', title)
    category_unit = re.search(r'\b' + UV_WORD + r'\b', path) and re.match(r'^' + UNIT + r'\b', title)
    if direct or reverse or category_unit:
        specs.update(kind='uv', embedded_role='')


def initialize(snapshot):
    snapshot['uv_version'] = 1
    sync(snapshot, create=True)


def sync(snapshot, create=False):
    if snapshot.get('uv_version') != 1:
        # A manual catalogue choice can introduce the new role into a legacy
        # estimate. Adopt its lifecycle without creating any additional rows.
        if not any(r['kind'] == 'uv' for v in snapshot['variants'].values() for b in v['blocks'] for r in b['rows']):
            return
        snapshot['uv_version'] = 1
    from .pool_estimates import make_row, EstimateInputError
    enabled = snapshot['inputs'].get('uv_enabled', False)
    for variant in snapshot['variants'].values():
        allrows = [r for block in variant['blocks'] for r in block['rows']]
        units = [r for r in allrows if r['kind'] == 'uv']
        if enabled and create and not units and not variant.get('uv_initialized'):
            equipment = next(b for b in variant['blocks'] if b['id'] == 'equipment')
            ids = {r['id'] for r in allrows}
            rid = next((key for key in ['uv', *('uv_' + str(n) for n in range(1, 100))] if key not in ids), None)
            if not rid or len(equipment['rows']) >= 100:
                raise EstimateInputError('В блоке оборудования недостаточно места для УФ-установки.')
            row = make_row(rid, TITLE, 'шт.', '1', kind='uv')
            equipment['rows'].append(row)
            units.append(row)
        if units:
            variant['uv_initialized'] = True
        for row in units:
            if not enabled and not row['excluded']:
                row.update(excluded=True, reason=OFF)
            elif enabled and row['excluded'] and row['reason'] == OFF:
                row.update(excluded=False, reason='')


def annotate(snapshot, variant):
    if snapshot.get('uv_version') != 1 or not snapshot['inputs'].get('uv_enabled'):
        return
    active = [r for b in variant['blocks'] if not b['excluded'] for r in b['rows'] if r['kind'] == 'uv' and not r['excluded']]
    if not active:
        variant['warnings'].append('УФ-установка включена в параметрах, но её строка удалена или исключена; проверьте состав сметы.')
    for row in active:
        note = CHECK if row.get('catalog') or row.get('model') else 'Выберите УФ-установку в каталоге.'
        origin = row.get('catalog')
        if origin and not origin.get('available'):
            from .estimate_availability import reason
            note += ' ' + reason(origin)
        if row.get('catalog_review_note'):
            note = row['catalog_review_note'] + ' ' + note
        row['selection_note'] = ('Проверить: ' + note)[:1000]
        row['compatibility'] = {'message': row['selection_note'], 'can_auto': False, 'kind': 'uv'}
        variant['warnings'].append(row['selection_note'])
