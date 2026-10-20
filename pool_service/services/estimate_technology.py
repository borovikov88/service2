"""Opt-in geometric quantities, not a structural or excavation stability design.

All layer thicknesses, projections and schemes must be supplied explicitly.
Values are exact Decimals; round upwards once, only when assigned to estimate rows.
"""
from decimal import Decimal as D

MODES = {
    'sand': {'', 'off', 'on'}, 'preparation': {'', 'off', 'on'},
    'formwork': {'', 'off', 'ground_two_sided'},
    'insulation_floor': {'', 'off', 'on'}, 'insulation_wall': {'', 'off', 'on'},
    'outer_finish': {'', 'off', 'on'}, 'top_finish': {'', 'off', 'on', 'film_band'},
    'plaster': {'', 'off', 'walls', 'walls_floor'},
    'pit': {'', 'off', 'vertical'}, 'backfill': {'', 'off', 'vertical'},
}
DIMENSIONS = ('sand_thickness', 'sand_projection', 'preparation_thickness', 'preparation_projection',
              'insulation_floor_thickness', 'insulation_wall_thickness', 'insulation_wall_height',
              'outer_finish_height', 'top_band_width', 'plaster_thickness', 'pit_gap', 'bowl_above_ground')
# code, mode, metric, title, unit, block, existing work code (None = new material)
ROWS = (
    ('sand_material', 'sand', 'sand_volume', 'Песчаная подготовка: материал', 'м³', 'construction', None),
    ('sand_work', 'sand', 'sand_area', 'Песчаная подготовка', 'м²', 'construction', 'sand_base'),
    ('preparation_material', 'preparation', 'preparation_volume', 'Бетонная подготовка: материал', 'м³', 'construction', None),
    ('preparation_work', 'preparation', 'preparation_volume', 'Бетонная подготовка: бетонирование', 'м³', 'construction', 'base_concrete'),
    ('formwork', 'formwork', 'formwork_area', 'Устройство опалубки', 'м²', 'construction', 'formwork'),
    ('strip_formwork', 'formwork', 'formwork_area', 'Снятие опалубки', 'м²', 'construction', 'strip_formwork'),
    ('insulation_floor', 'insulation_floor', 'insulation_floor_area', 'Утеплитель под плитой: материал', 'м²', 'construction', None),
    ('insulation_wall', 'insulation_wall', 'insulation_wall_area', 'Утеплитель снаружи стен: материал', 'м²', 'construction', None),
    ('insulation_work', 'insulation', 'insulation_area', 'Утепление', 'м²', 'construction', 'insulation'),
    ('outer_finish_material', 'outer_finish', 'outer_finish_area', 'Наружная отделка: материал', 'м²', 'finish', None),
    ('outer_finish_work', 'outer_finish', 'outer_finish_area', 'Наружная отделка: работа', 'м²', 'finish', 'outer_finish'),
    ('top_finish_material', 'top_finish', 'top_finish_area', 'Отделка верхнего кольца: материал', 'м²', 'finish', None),
    ('top_finish_work', 'top_finish', 'top_finish_area', 'Отделка верхнего кольца: работа', 'м²', 'finish', 'top_finish'),
    ('plaster_material', 'plaster', 'plaster_volume', 'Штукатурный слой: объём (не расход сухой смеси)', 'м³', 'finish', None),
    ('plaster_work', 'plaster', 'plaster_area', 'Штукатурка', 'м²', 'finish', 'plaster'),
    ('pit', 'pit', 'pit_volume', 'Выемка грунта', 'м³', 'construction', 'excavation'),
    ('backfill', 'backfill', 'backfill_volume', 'Обратная засыпка', 'м³', 'construction', 'backfill'),
)
BASES = {'tech_' + r[2] for r in ROWS}
OFF = 'Отключено в строительной технологии'


def fresh_defaults(raw):
    defaults = {'tech_sand':'on','tech_sand_thickness':'.10','tech_sand_projection':'0',
        'tech_preparation':'on','tech_preparation_thickness':'.15','tech_preparation_projection':'0',
        'tech_insulation_floor':'off','tech_insulation_floor_thickness':'.05',
        'tech_insulation_wall':'on','tech_insulation_wall_thickness':'.05',
        'tech_insulation_wall_height_mode':'auto' if 'tech_insulation_wall_height' not in raw else 'manual',
        'tech_outer_finish':'off','tech_top_finish':'film_band' if raw.get('finish')=='film' else 'off',
        'tech_top_band_width':'.05','tech_plaster':'walls_floor','tech_plaster_thickness':'.02',
        'tech_pit':'vertical','tech_pit_gap':'.50','tech_bowl_above_ground':'.30','tech_backfill':'vertical'}
    return {**defaults, **raw}


def band_area(inputs):
    if mode(inputs,'top_finish') != 'film_band' or inputs.get('finish') != 'film': return D(0)
    vals = [inputs.get(k) for k in ('length','width','wall_thickness','bowl_height','tech_top_band_width')]
    if any(v in (None,'') for v in vals): return None
    L,W,t,H,w = map(D, vals)
    if w > t: return None
    return (L+2*w)*(W+2*w)-L*W


def normalize(raw):
    from .pool_estimates import number, EstimateInputError
    result = {}
    for name, choices in MODES.items():
        key = 'tech_' + name
        if key in raw:
            if not isinstance(raw[key], str) or raw[key] not in choices:
                raise EstimateInputError('Выберите строительную технологию: ' + name)
            result[key] = raw[key]
    for name in DIMENSIONS:
        key = 'tech_' + name
        if key in raw:
            minimum = '0' if name.endswith('projection') or name in ('pit_gap', 'bowl_above_ground') else '.001'
            value = number(raw[key], 'Строительная геометрия: ' + name, optional=True, minimum=minimum, maximum='20')
            result[key] = str(value) if value is not None else None
    if 'tech_insulation_wall_height_mode' in raw:
        auto = raw['tech_insulation_wall_height_mode']
        if auto not in ('','auto','manual'): raise EstimateInputError('Выберите режим высоты утепления.')
        result['tech_insulation_wall_height_mode'] = auto
    height = raw.get('bowl_height')
    if height not in (None, ''):
        height = number(height, 'Высота чаши', minimum='.001', maximum='10')
        if result.get('tech_insulation_wall_height_mode') == 'auto':
            if height <= D('.10'): raise EstimateInputError('Для автоматической высоты утепления высота чаши должна быть больше 0,10 м.')
            result['tech_insulation_wall_height'] = str(height-D('.10'))
        for name in ('insulation_wall_height', 'outer_finish_height', 'bowl_above_ground'):
            if result.get('tech_' + name) and D(result['tech_' + name]) > height:
                raise EstimateInputError('Высота слоя/борта над грунтом не может превышать высоту чаши.')
    elif result.get('tech_insulation_wall_height_mode') == 'auto':
        result['tech_insulation_wall_height'] = None
    if result.get('tech_top_finish') == 'film_band' and result.get('tech_top_band_width') and raw.get('wall_thickness'):
        if D(result['tech_top_band_width']) > D(str(raw['wall_thickness'])):
            raise EstimateInputError('Заход плёнки не может быть шире толщины стены.')
    return result


def mode(inputs, name):
    if name != 'insulation':
        return inputs.get('tech_' + name, '')
    choices = [inputs.get('tech_insulation_' + x, '') for x in ('floor', 'wall')]
    return 'on' if 'on' in choices else 'off' if choices == ['off', 'off'] else ''


def quantities(inputs):
    def value(name):
        raw = inputs.get(name)
        return D(raw) if raw not in (None, '') else None
    values = {basis: None for basis in BASES}
    def put(name, val): values['tech_' + name] = format(val, 'f') if val is not None else None
    L, W, H, t, b = (value(k) for k in ('length', 'width', 'bowl_height', 'wall_thickness', 'slab_thickness'))
    if any(x is None for x in (L, W, H, t, b)):
        return values
    Lo, Wo = L + 2*t, W + 2*t
    Ai, Ao, Pi, Po = L*W, Lo*Wo, 2*(L+W), 2*(Lo+Wo)
    layers = {}
    for name in ('sand', 'preparation'):
        state = mode(inputs, name)
        thick, projection = value('tech_' + name + '_thickness'), value('tech_' + name + '_projection')
        if state == 'off': layers[name] = (D(0), Ao)
        elif state == 'on' and thick is not None and projection is not None:
            area = (Lo+2*projection)*(Wo+2*projection)
            put(name+'_area', area); put(name+'_volume', area*thick)
            layers[name] = (thick, area)
    if mode(inputs,'formwork') == 'ground_two_sided':
        put('formwork_area', (Pi+Po)*H+Po*b)
    if mode(inputs,'insulation_floor') == 'on' and value('tech_insulation_floor_thickness') is not None:
        put('insulation_floor_area', Ao)
    h = value('tech_insulation_wall_height')
    if mode(inputs,'insulation_wall') == 'on' and h is not None and value('tech_insulation_wall_thickness') is not None:
        put('insulation_wall_area', Po*h)
    insulation = [D(0) if mode(inputs,'insulation_'+part) == 'off' else
                  D(values['tech_insulation_'+part+'_area']) if values['tech_insulation_'+part+'_area'] is not None else None
                  for part in ('floor','wall')]
    if all(x is not None for x in insulation): put('insulation_area', sum(insulation))
    h = value('tech_outer_finish_height')
    if mode(inputs,'outer_finish') == 'on' and h is not None: put('outer_finish_area', Po*h)
    if mode(inputs,'top_finish') == 'on': put('top_finish_area', Ao-Ai)
    if mode(inputs,'plaster') in ('walls','walls_floor'):
        area = Pi*H + (Ai if mode(inputs,'plaster') == 'walls_floor' else 0)
        put('plaster_area', area)
        thick = value('tech_plaster_thickness')
        if thick is not None: put('plaster_volume', area*thick)
    # Vertical prism; subtract insulation actually below ground, including corners.
    gap, z = value('tech_pit_gap'), value('tech_bowl_above_ground')
    simple = (mode(inputs,'pit') == 'vertical' and gap is not None and z is not None
              and len(layers) == 2 and mode(inputs,'outer_finish') == 'off'
              and all(mode(inputs,k) in ('on','off') for k in ('insulation_floor','insulation_wall')))
    if simple:
        sand, As = layers['sand']; concrete, Ac = layers['preparation']
        Ap = (Lo+2*gap)*(Wo+2*gap)
        floor = D(0) if mode(inputs,'insulation_floor') == 'off' else value('tech_insulation_floor_thickness')
        wall = D(0) if mode(inputs,'insulation_wall') == 'off' else value('tech_insulation_wall_thickness')
        wh = D(0) if mode(inputs,'insulation_wall') == 'off' else value('tech_insulation_wall_height')
        if (floor is not None and wall is not None and wh is not None and wall <= gap
                and As <= Ap and Ac <= Ap):
            depth = H-z
            put('pit_volume', Ap*(depth+b+concrete+sand+floor))
            if mode(inputs,'backfill') == 'vertical':
                # Wall insulation starts at top of slab; only submerged height displaces fill.
                displacement = ((Lo+2*wall)*(Wo+2*wall)-Ao)*min(wh,depth)
                fill = (Ap-As)*sand+(Ap-Ac)*concrete+(Ap-Ao)*(floor+b+depth)-displacement
                put('backfill_volume', fill if fill >= 0 else None)
    return values


def bind_new_work(row):
    """New work rows require an explicit scheme; historical rows never call this."""
    for _, _, metric, _, unit, _, work_code in ROWS:
        if work_code and row.get('work_code') == work_code and row['unit'] == unit:
            row.update(basis='tech_' + metric, auto_quantity=True, quantity=None)
            break


def initialize_fresh(snapshot):
    """Bind only freshly generated work rows; never call for saved snapshots."""
    snapshot['technology_version'] = 1
    for variant in snapshot['variants'].values():
        for _, _, metric, _, unit, block_id, work_code in ROWS:
            if not work_code:
                continue
            block = next(b for b in variant['blocks'] if b['id'] == block_id)
            for row in block['rows']:
                if row.get('work_code') == work_code and row['unit'] == unit:
                    row.update(basis='tech_' + metric, auto_quantity=True, quantity=None)


def apply(snapshot, create=False):
    from .pool_estimates import make_row, EstimateInputError
    from .estimate_embedded import ceil_quantity
    inputs = snapshot['inputs']
    if not any(inputs.get('tech_' + name) for name in MODES) and not any(
            r['basis'] in BASES for v in snapshot['variants'].values() for b in v['blocks'] for r in b['rows']):
        return {}
    metrics = quantities(inputs)
    for variant in snapshot['variants'].values():
        for code, feature, metric, title, unit, block_id, work_code in ROWS:
            if snapshot.get('construction_version') and block_id == 'construction': continue
            state, basis = mode(inputs, feature), 'tech_' + metric
            if feature == 'top_finish' and state == 'film_band': state = 'off'  # Included once in main PVC finish rows.
            block = next(b for b in variant['blocks'] if b['id'] == block_id)
            matches = [r for r in block['rows'] if r['id'] == 'tech_'+code or
                       (work_code and r.get('work_code') == work_code)]
            if len(matches) > 1: continue  # Ambiguous user duplicates must not be guessed.
            row = matches[0] if matches else None
            if not state:
                # Only rows previously bound to this scheme become unknown. Legacy work is frozen.
                if row and row['auto_quantity'] and row['basis'] == basis and row['unit'] == unit and not row['existing']:
                    row['quantity'] = None
                    if row['excluded'] and row['reason'] == OFF: row.update(excluded=False, reason='')
                continue
            if row is None and create and state != 'off':
                if len(block['rows']) >= 100: raise EstimateInputError('В блоке недостаточно места для строительных позиций.')
                row = make_row('tech_'+code, title, unit, None, basis, 'work' if work_code else 'other')
                if work_code: row['work_code'] = work_code
                block['rows'].append(row)
            if row is None or row['existing'] or row['unit'] != unit: continue
            if state == 'off':
                if not row['excluded']: row.update(excluded=True, reason=OFF)
                continue
            if row['excluded'] and row['reason'] == OFF: row.update(excluded=False, reason='')
            eligible = (row['auto_quantity'] and row['basis'] == basis)
            if create and not row.get("selection_locked") and ((row['quantity'] is None and not row['basis']) or
                           (work_code == 'plaster' and row['auto_quantity'] and row['basis'] == 'finish_net')):
                row.update(basis=basis, auto_quantity=True); eligible=True
            if eligible and row['unit'] == unit:
                row['quantity'] = format(D(ceil_quantity(metrics[basis])), '.3f') if metrics[basis] is not None else None
    return metrics if any(inputs.get('tech_' + name) for name in MODES) else {}


def warnings(inputs):
    active = [key for key in MODES if mode(inputs,key) not in ('','off')]
    if not active: return []
    result = ['Строительные количества — геометрия выбранной технологии, без подтверждения прочности/армирования. Цены материалов и расход сухих смесей не предполагаются.']
    metrics = quantities(inputs)
    if mode(inputs,'top_finish') == 'film_band' and band_area(inputs) is None:
        result.append('Заход плёнки пока не включён в количество: уточните высоту чаши, толщину стен и ширину полосы.')
    if any(metrics['tech_'+metric] is None for _,feature,metric,*_ in ROWS if mode(inputs,feature) not in ('','off','film_band')):
        result.append('Часть строительных объёмов неизвестна: заполните размеры/слои и явно отключите отсутствующие. Для котлована/засыпки модель только с вертикальными стенками, без наружной отделки; утепление стен от верха плиты, подготовки должны помещаться внутри котлована.')
    if 'pit' in active or 'backfill' in active:
        result.append('Котлован: только выбранная геометрическая модель вертикальных стенок. Устойчивость грунта, откосы, крепления, водопонижение и возможность работ требуют отдельного проекта; объём грунта без коэффициентов разрыхления/уплотнения.')
    return result


def summary(inputs):
    """Printable scheme and exact input dimensions, frozen with each saved version."""
    labels = {
        'sand':'Песчаная подготовка', 'preparation':'Бетонная подготовка', 'formwork':'Опалубка',
        'insulation_floor':'Утепление под плитой', 'insulation_wall':'Утепление стен',
        'outer_finish':'Наружная отделка', 'top_finish':'Отделка верхнего кольца', 'plaster':'Штукатурка',
        'pit':'Котлован', 'backfill':'Обратная засыпка',
        'sand_thickness':'Толщина песка, м', 'sand_projection':'Выпуск песка за плиту, м',
        'preparation_thickness':'Толщина бетонной подготовки, м', 'preparation_projection':'Выпуск бетонной подготовки, м',
        'insulation_floor_thickness':'Толщина утеплителя под плитой, м', 'insulation_wall_thickness':'Толщина утеплителя стен, м',
        'insulation_wall_height':'Высота утепления стен, м', 'outer_finish_height':'Высота наружной отделки, м',
        'top_band_width':'Ширина верхнего захода плёнки, м',
        'insulation_wall_height_mode':'Режим высоты утепления',
        'plaster_thickness':'Толщина штукатурки, м', 'pit_gap':'Зазор котлована за плитой, м',
        'bowl_above_ground':'Верх чаши над грунтом, м',
    }
    modes = {'auto':'Автоматически H − 0,10 м','manual':'Ручная высота','film_band':'Полоса ПВХ в общей площади отделки', 'off':'Не включать','on':'Включить','ground_two_sided':'Две стороны стен + торец плиты, плита на грунте',
             'walls':'Стены','walls_floor':'Стены и дно','vertical':'Вертикальная прямоугольная модель'}
    return [{'label':label,'value':modes.get(inputs['tech_'+key],inputs['tech_'+key])}
            for key,label in labels.items() if inputs.get('tech_'+key) not in (None,'')]
