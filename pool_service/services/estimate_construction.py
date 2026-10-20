"""New-estimate construction package. Quantities are estimates, not structural design."""
from decimal import Decimal as D, ROUND_CEILING, ROUND_HALF_UP

MATERIAL_BLOCK = 'construction_materials'
QUANTITY_STEPS = {'pkg_concrete_total': D('1'), 'pkg_base_volume': D('1'),
                  'pkg_sand_tonnes': D('1'), 'pkg_rebar_mass': D('0.5')}

def estimate_quantity(value, step):
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step if value is not None else None


PRICE_BASES = {'concrete_bowl': ('8000','1'), 'concrete_base': ('7700','1'),
               'sand': ('950','1'), 'rebar': ('75','1'), 'insulation': ('2772','4.85')}
PARAMETERS = {'sand_density':('1.6','.1','5'), 'rebar_diameter':(None,'6','40'),
              'rebar_step':('.2','.05','1'), 'rebar_layers':('2','1','4'),
              'base_rebar_step':('.3','.05','1'), 'base_rebar_layers':('1','1','4'),
              'rebar_reserve':('5','0','50'), 'supplies_rate':('250','0','1000000')}
MATERIALS = (
    ('concrete_bowl','Бетон М300 для дна и стен чаши','м³','concrete_total'),
    ('concrete_base','Бетон М250 для подготовки','м³','base_volume'),
    ('sand','Песок для подготовки','т','sand_tonnes'),
    ('rebar','Арматура А500С — оценка количества','кг','rebar_mass'),
    ('insulation','Пеноплэкс Фундамент 50 мм','м²','insulation_area'),
    ('supplies','Расходные материалы для устройства чаши','компл.','package_count'),
)
WORKS = (
    ('bowl_package','Устройство бетонной чаши','м³','concrete_total','20000'),
    ('excavation_package','Выемка грунта','м³','pit_volume',None),
    ('soil_removal','Вывоз грунта','м³','soil_removal',None),
    ('handling_package','Погрузочно-разгрузочные работы','т','handling','300'),
    ('delivery_concrete','Доставка бетона','рейс','',None),
    ('delivery_rebar','Доставка арматуры','рейс','',None),
    ('delivery_sand','Доставка песка','рейс','',None),
)
EXPECTED_UNITS = {'pkg_'+metric:unit for _,_,unit,metric in MATERIALS}
EXPECTED_UNITS.update({'pkg_'+r[3]:r[2] for r in WORKS if r[3]})
BASES = {'pkg_'+metric for *_,metric in MATERIALS} | {'pkg_'+r[3] for r in WORKS if r[3]}
INTERNAL = ('Арматура: предварительная сметная модель количества, не инженерный подбор. '
            'Диаметр и сетки обязательно проверить с конструктором. Нахлёсты, анкеровки, '
            'защитные слои и достаточность схемы не подтверждены; отдельно не учтены.')
CLIENT_NOTE = ('Стоимость выемки и вывоза грунта зависит от способа выемки, возможности подъезда техники '
               'и места размещения/утилизации грунта.')


def normalize(raw):
    from .pool_estimates import number, EstimateInputError
    out={}
    if 'construction_scheme' in raw:
        if raw['construction_scheme'] not in ('legacy','package'): raise EstimateInputError('Неизвестная схема строительства.')
        out['construction_scheme']=raw['construction_scheme']
    for key,(_,minimum,maximum) in PARAMETERS.items():
        field='construction_'+key
        if field in raw:
            val=number(raw[field],field,optional=True,minimum=minimum,maximum=maximum)
            if key.endswith('layers') and val is not None and val != val.to_integral_value():
                raise EstimateInputError('Количество слоёв сетки должно быть целым.')
            out[field]=str(val) if val is not None else None
    for field,choices in {'construction_diameter_mode':('auto','manual',''),
                          'construction_soil_reuse':('suitable_all','unknown','')}.items():
        if field in raw:
            if raw[field] not in choices: raise EstimateInputError('Уточните сметное допущение.')
            out[field]=raw[field]
    return out


def defaults(raw):
    return {'construction_scheme':'package', **{'construction_'+k:v[0] for k,v in PARAMETERS.items()},
            'construction_diameter_mode':'auto', 'construction_soil_reuse':'suitable_all', **raw}


def mesh(x,y,step,layers):
    return (((x/step).to_integral_value(rounding=ROUND_CEILING)+1)*y+
            ((y/step).to_integral_value(rounding=ROUND_CEILING)+1)*x)*layers


def metrics(inputs, geometry):
    from .estimate_technology import quantities, mode
    tech=quantities(inputs)
    def val(key):
        raw=inputs.get(key); return D(raw) if raw not in (None,'') else None
    def metric(key):
        raw=geometry.get(key); return D(raw) if raw is not None else None
    def techval(key,feature):
        if mode(inputs,feature)=='off': return D(0)
        raw=tech.get('tech_'+key);return D(raw) if raw is not None else None
    out={'concrete_total':metric('concrete_total'), 'base_volume':techval('preparation_volume','preparation'),
         'pit_volume':techval('pit_volume','pit'), 'insulation_area':techval('insulation_area','insulation')}
    sand=techval('sand_volume','sand');density=val('construction_sand_density')
    out['sand_tonnes']=sand*density if sand is not None and density is not None else None
    fill=techval('backfill_volume','backfill')
    out['soil_removal']=max(out['pit_volume']-fill,D(0)) if inputs.get('construction_soil_reuse')=='suitable_all' and out['pit_volume'] is not None and fill is not None else None
    L,W,H,t=(val(k) for k in ('length','width','bowl_height','wall_thickness'))
    diameter=(D(10) if max(L,W)<=10 else D(12)) if inputs.get('construction_diameter_mode')=='auto' else val('construction_rebar_diameter')
    step,layers,base_step,base_layers,reserve=(val('construction_'+k) for k in ('rebar_step','rebar_layers','base_rebar_step','base_rebar_layers','rebar_reserve'))
    out['package_count']=D(1)
    out['internal_area']=L*W+2*(L+W)*H if H is not None else None
    out.update(rebar_mass=None,rebar_base_mass=None,rebar_diameter=diameter)
    if all(v is not None for v in (L,W,H,t,diameter,step,layers,reserve)):
        length=mesh(L+2*t,W+2*t,step,layers)+2*(mesh(L+t,H,step,layers)+mesh(W+t,H,step,layers))
        projection=val('tech_preparation_projection')
        prep=mode(inputs,'preparation')
        if prep=='off': prep_length=D(0)
        elif prep=='on' and all(v is not None for v in (projection,base_step,base_layers)):
            prep_length=mesh(L+2*t+2*projection,W+2*t+2*projection,base_step,base_layers)
        else: prep_length=None
        if prep_length is not None:
            base=(length+prep_length)*D('3.141592653589793')*(diameter/1000)**2/4*D('7850')
            out['rebar_base_mass']=base
            out['rebar_mass']=base*(1+reserve/100)
    return out


def initialize(snapshot):
    from .pool_estimates import make_row
    for variant in snapshot['variants'].values():
        block=next(b for b in variant['blocks'] if b['id']=='construction')
        for code,title,unit,metric,price in WORKS:
            row=make_row(code,title,unit,None,'pkg_'+metric if metric else '', 'work')
            row.update(work_code=code,price=price)
            block['rows'].append(row)
        materials=next(b for b in variant['blocks'] if b['id']==MATERIAL_BLOCK)
        for code,title,unit,metric in MATERIALS:
            row=make_row('material_'+code,title,unit,None,'pkg_'+metric)
            if code in PRICE_BASES:
                retail,area=PRICE_BASES[code]
                client=D(retail)*D('1.20')
                row['construction_price']={'retail':retail,'markup':'20','divisor':area}
                row['price']=str((client/D(area)).quantize(D('.01'),rounding=ROUND_HALF_UP))
            row['handling_mode']='none' if code.startswith('concrete_') else 'unknown'
            materials['rows'].append(row)


def normalize_price(raw):
    from .pool_estimates import number, EstimateInputError
    if raw is None:return None
    if not isinstance(raw,dict):raise EstimateInputError('Некорректная исходная цена материала.')
    return {k:str(number(raw.get(k),k,minimum=minimum,maximum=maximum,places=6)) for k,minimum,maximum in
            (('retail','0','1000000000'),('markup','0','100'),('divisor','.001','1000000'))}


def price_active(row):
    p=row.get('construction_price')
    return bool(p and row['price'] is not None and not row.get('unit_price_manual') and
                D(row['price'])==(D(p['retail'])*(1+D(p['markup'])/100)/D(p['divisor'])).quantize(D('.01'),rounding=ROUND_HALF_UP))


def amount(row):
    p=row['construction_price']
    return (D(row['quantity'])*D(p['retail'])*(1+D(p['markup'])/100)/D(p['divisor'])).quantize(D('.01'),rounding=ROUND_HALF_UP)


def sync(snapshot):
    if snapshot.get('construction_version')!=1:return
    values=metrics(snapshot['inputs'],snapshot['geometry'])
    snapshot['construction_assumptions']={k:str(v) if v is not None else None for k,v in values.items()}
    for variant in snapshot['variants'].values():
        notes=[INTERNAL, 'Песок: условный коэффициент '+str(snapshot['inputs'].get('construction_sand_density') or 'не задан')+' т/м³, без дополнительного запаса.']
        handling=D(0);confirmed=False;incomplete=False
        for block in variant['blocks']:
            for row in block['rows']:
                row['construction_quantity_note']=''
                row['construction_calculated_quantity']=None
                key=row['basis'].removeprefix('pkg_')
                expected_unit=EXPECTED_UNITS.get(row['basis'])
                mismatch=bool(expected_unit and row['unit']!=expected_unit)
                row['construction_unit_conflict']=mismatch and not (not row['auto_quantity'] and
                    (row.get('unit_price_manual') or row.get('rate_source')=='Ручная цена текущей сметы'))
                if mismatch:
                    if row['auto_quantity'] and not row['existing']:row['quantity']=None
                    notes.append(f"{row['name']}: автоматическая база имеет единицу {expected_unit}, а строка — {row['unit']}. Задайте количество и цену вручную либо восстановите единицу; автоматическое начисление не подтверждено.")
                if not mismatch and row['basis'] in BASES and key in values and row['auto_quantity'] and not row['existing']:
                    value=values[key]
                    if snapshot.get('construction_quantity_version') == 1 and block['id'] == MATERIAL_BLOCK and row['basis'] in QUANTITY_STEPS:
                        row['construction_calculated_quantity']=format(value,'f') if value is not None else None
                        step=QUANTITY_STEPS[row['basis']]
                        estimated=estimate_quantity(value,step)
                        if value is not None:
                            shown=format(value.quantize(D('.1'),rounding=ROUND_HALF_UP),'f')
                            row['construction_quantity_note']=(f'Расчётное количество ≈ {shown} {row["unit"]}; '
                                f'сметное {estimated} {row["unit"]}: округление вверх до {step} {row["unit"]}. '
                                'Стоимость материала рассчитана по сметному количеству.')
                        value=estimated
                    row['quantity']=format(value,'f') if value is not None else None
                if row['id']=='material_insulation' and row.get('construction_price') and not row.get('unit_price_manual'):
                    inputs=snapshot['inputs']
                    compatible=all(inputs.get('tech_insulation_'+part)=='off' or
                        inputs.get('tech_insulation_'+part)=='on' and inputs.get('tech_insulation_'+part+'_thickness') is not None and
                        D(inputs['tech_insulation_'+part+'_thickness'])==D('.05') for part in ('floor','wall'))
                    if not compatible and (price_active(row) or row.get('construction_price_suspended')):
                        row['price']=None;row['construction_price_suspended']=True
                        notes.append('Цена Пеноплэкса относится к 50 мм; для выбранной/неизвестной толщины цена не начислена. Уточните материал и цену вручную.')
                    elif compatible and row.get('construction_price_suspended'):
                        p=row['construction_price']
                        row['price']=str((D(p['retail'])*(1+D(p['markup'])/100)/D(p['divisor'])).quantize(D('.01'),rounding=ROUND_HALF_UP))
                        row['construction_price_suspended']=False
                if row['id']=='material_supplies' and not row.get('catalog') and not mismatch:
                    previous=row.get('construction_computed_price')
                    rate=snapshot['inputs'].get('construction_supplies_rate')
                    value=values['internal_area']*D(rate) if values['internal_area'] is not None and rate is not None else None
                    expected=str(value.quantize(D('.01'),rounding=ROUND_HALF_UP)) if value is not None else None
                    if not row.get('unit_price_manual') and (row['price']==previous or row['price'] is None and previous is None): row['price']=expected
                    row['construction_computed_price']=expected
                p=row.get('construction_price')
                row['construction_price_note']=(f"Исходная цена {p['retail']} ₽ / {p['divisor']} единиц; наценка {p['markup']}%. "
                    + ('Расчёт без промежуточного округления цены.' if price_active(row) else 'В строке ручная цена; наценка повторно не применяется.')) if p else ''
                if block['id']!=MATERIAL_BLOCK or block['excluded'] or row['excluded'] or row['existing']:continue
                if row['id'] in ('material_concrete_bowl','material_concrete_base'):continue
                mode=row.get('handling_mode','unknown')
                if mode=='unknown':incomplete=True
                if mode!='manual':continue
                if row.get('construction_unit_conflict'):incomplete=True;continue
                mass=row.get('handling_mass')
                if mass is None and row['quantity'] is not None and row['unit'] in ('т','кг'):
                    mass=D(row['quantity'])/(1000 if row['unit']=='кг' else 1)
                if mass is None:incomplete=True
                else:handling+=D(mass);confirmed=True
        for block in variant['blocks']:
            for row in block['rows']:
                if row['basis']=='pkg_handling' and row['unit']=='т' and row['auto_quantity'] and not row['existing']:
                    row['quantity']=format(handling,'.6f') if confirmed or not incomplete else None
        if incomplete:notes.append('ПРР: база неполная — учитывается только подтверждённая ручная разгрузка с известной массой. Товарный бетон исключён. Самосвальная/механизированная разгрузка не начисляется как ручная.')
        notes.append('Вывоз: котлован минус обратная засыпка только при пригодности всего оставляемого грунта; объёмы в плотном теле, без коэффициентов разрыхления/уплотнения. Подтвердите допущение или исправьте количество вручную.')
        variant['construction_notes']=notes
