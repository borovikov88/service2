from copy import deepcopy
from decimal import Decimal
from django.test import SimpleTestCase
from pool_service.services import pool_estimates as engine, estimate_layout as layout
from pool_service.services.estimate_level_shared import filtration_quantity
from .test_estimate_construction import NEW


def row(s,kind,tier='economy'):
    return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['kind']==kind)


def byid(s,rid,tier='economy'):
    return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['id']==rid)

class TwoStageTests(SimpleTestCase):
    def test_exact_structure_and_single_labor_table(self):
        s=engine.generate(NEW)
        for v in s['variants'].values():
            sections=layout.sections(v)
            self.assertEqual([[b['title'] for b in st['blocks']] for st in sections],[['Работы по строительству чаши','Материалы для строительства чаши'],['Закладные элементы','Оборудование','Отделочные материалы','Отделочные работы, установка закладных и монтаж оборудования','Дополнительные материалы','Дополнительные работы']])
            work=sections[1]['blocks'][3]['rows']
            for rid in ('embedded_work','equipment_work','finish_work','work_finish_travel'):
                self.assertIn(rid,[r['id'] for r in work])
            self.assertIn('work_accommodation',[r['id'] for r in sections[1]['blocks'][5]['rows']])
            self.assertIn('delivery_concrete',[r['id'] for r in sections[0]['blocks'][0]['rows']])
            self.assertIn('piping',[r['id'] for r in sections[1]['blocks'][2]['rows']])
            ids=[r['id'] for st in sections for b in st['blocks'] for r in b['rows']]
            self.assertEqual(len(ids),len(set(ids)))
            self.assertEqual(sum(Decimal(b['subtotal']) for st in sections for b in st['blocks']),Decimal(v['total']))

    def test_additional_rows_roundtrip_and_stage_one_exclusion(self):
        s=engine.generate(NEW)
        for v in s['variants'].values():
            b=next(b for b in v['blocks'] if b['id']=='options')
            b['rows'] += [dict(engine.make_row('custom_material','Материал'),price='123',quantity='2'),
                          dict(engine.make_row('custom_work','Работа',kind='work'),price='456',quantity='3')]
            v['view_order']={'extra_materials':['custom_material'],'extra_work':['custom_work']}
        s=engine.evaluate(s)
        original=deepcopy(s)
        for b in s['variants']['economy']['blocks']:
            if b['id'] in {'construction','construction_materials'}:b['excluded']=True
        s=engine.recalculate(s,s['inputs'])
        sections=layout.sections(s['variants']['economy'],client=True)
        self.assertNotIn('stage1',[st['id'] for st in sections])
        stage2=next(st for st in sections if st['id']=='stage2')
        self.assertEqual(next(b for b in stage2['blocks'] if b['id']=='extra_materials')['rows'][0]['id'],'custom_material')
        self.assertEqual(next(b for b in stage2['blocks'] if b['id']=='extra_work')['rows'][0]['id'],'custom_work')
        self.assertEqual(byid(s,'custom_material')['amount'],'246.00')
        self.assertEqual(byid(s,'custom_work')['amount'],'1368.00')
        self.assertEqual(s['variants']['standard']['total'],original['variants']['standard']['total'])

    def test_installation_actual_six_including_manual_and_excluded(self):
        s=engine.generate(NEW)
        for b in s['variants']['economy']['blocks']:
            for r in b['rows']:
                if r['kind'] in {'vacuum','pool_light','light_niche','light_box','level_regulator'}:r['excluded']=True
        for kind,qty in [('skimmer','1'),('inlet','3'),('drain','2')]:row(s,kind).update(quantity=qty,auto_quantity=False)
        row(s,'inlet').update(filtration_quantity='3',level_quantity='0')
        byid(s,'embedded_work')['price']='1000'
        s=engine.evaluate(s)
        self.assertEqual(Decimal(byid(s,'embedded_work')['quantity']),6)
        self.assertEqual(byid(s,'embedded_work')['amount'],'6000.00')
        row(s,'drain')['excluded']=True
        self.assertEqual(Decimal(byid(engine.evaluate(s),'embedded_work')['quantity']),4)

    def test_lamp_niche_one_point_and_box_extra(self):
        s=engine.generate({**NEW,'lighting':'yes','light_count':'2'})
        before=Decimal(byid(engine.generate(NEW),'embedded_work')['quantity'])
        self.assertEqual(Decimal(byid(s,'embedded_work')['quantity']),before+4)
        row(s,'light_niche').update(excluded=True,reason='В комплекте')
        self.assertEqual(Decimal(byid(engine.evaluate(s),'embedded_work')['quantity']),before+4)

    def test_shared_one_automatic_for_multiple_models_no_repeated_plus_one(self):
        s=engine.generate(NEW)
        original=row(s,'inlet');other=deepcopy(original);other.update(id='another_inlet',quantity='2',auto_quantity=False,filtration_quantity='2',level_quantity='0')
        next(b for b in s['variants']['economy']['blocks'] if b['id']=='embedded')['rows'].append(other)
        original.update(quantity='3',auto_quantity=False,filtration_quantity='3',level_quantity='0')
        for _ in range(3):s=engine.recalculate(s,s['inputs'])
        inlets=[r for b in s['variants']['economy']['blocks'] for r in b['rows'] if r['kind']=='inlet' and not r['excluded']]
        self.assertEqual(sum(Decimal(r['level_quantity']) for r in inlets),1)
        self.assertEqual(sum(Decimal(r['quantity']) for r in inlets),6)
        self.assertEqual(sum(filtration_quantity(r) for r in inlets),5)

    def test_corner_whole_two_meter_pieces_and_manual_price(self):
        s=engine.generate(NEW)
        r=byid(s,'liner_corner_2m');self.assertEqual(r['quantity'],'12');self.assertEqual(r['unit'],'шт.');self.assertIsNone(r['price'])
        r['price']='900';r['unit_price_manual']=True
        s=engine.recalculate(s,{**s['inputs'],'width':'4.1'})
        self.assertEqual(byid(s,'liner_corner_2m')['quantity'],'13')
        self.assertEqual(byid(s,'liner_corner_2m')['amount'],'11700.00')
        self.assertEqual(byid(engine.evaluate(s),'liner_corner_2m')['quantity'],'13')
        byid(s,'liner_corner_2m').update(quantity='7',auto_quantity=False)
        self.assertEqual(byid(engine.recalculate(s,{**s['inputs'],'length':'12'}),'liner_corner_2m')['quantity'],'7.000')

    def test_view_order_survives_recalc_and_deleted_entries_are_pruned(self):
        s=engine.generate(NEW);v=s['variants']['economy']
        ids=[r['id'] for r in layout.sections(v)[1]['blocks'][3]['rows']]
        v['view_order']={'installation_work':list(reversed(ids))}
        s=engine.recalculate(s,s['inputs']);self.assertEqual([r['id'] for r in layout.sections(s['variants']['economy'])[1]['blocks'][3]['rows']],list(reversed(ids)))
        for b in s['variants']['economy']['blocks']:b['rows'][:]=[r for r in b['rows'] if r['id']!='work_finish_travel']
        self.assertNotIn('work_finish_travel',engine.evaluate(s)['variants']['economy']['view_order']['installation_work'])

    def test_legacy_package_price_is_never_multiplied_by_points(self):
        s=engine.generate(NEW)
        target=byid(s,'embedded_work')
        target.update(basis='',unit='компл.',quantity='1',auto_quantity=False,price='10000',unit_price_manual=True,selection_locked=True)
        before=engine.evaluate(s)
        after=engine.recalculate(before,before['inputs'])
        self.assertEqual(byid(after,'embedded_work')['amount'],'10000.00')
        self.assertEqual(byid(after,'embedded_work')['unit'],'компл.')
        self.assertFalse(byid(after,'embedded_work')['auto_quantity'])

    def test_old_bound_scheme_only_upgraded_by_explicit_current_ui_mode(self):
        s=engine.generate(NEW);s.pop('level_shared_quantity_version')
        before=deepcopy(s)
        self.assertNotIn('level_shared_quantity_version',engine.evaluate(s))
        self.assertNotIn('level_shared_quantity_version',engine.recalculate(s,s['inputs']))
        upgraded=engine.recalculate(s,{**s['inputs'],'level_inlet_mode':'automatic'})
        self.assertEqual(upgraded['level_shared_quantity_version'],2)
        self.assertEqual(s,before)

    def test_corner_delete_and_finish_toggles_preserve_manual_exclusion(self):
        s=engine.generate(NEW)
        original=byid(s,'liner_corner_2m')
        original.update(price='0',unit_price_manual=True,excluded=True,reason='Исключено пользователем')
        s=engine.recalculate(s,{**s['inputs'],'finish':'mosaic'})
        s=engine.recalculate(s,{**s['inputs'],'finish':'film'})
        self.assertTrue(byid(s,'liner_corner_2m')['excluded'])
        self.assertEqual(byid(s,'liner_corner_2m')['price'],'0.00')
        for b in s['variants']['economy']['blocks']:b['rows'][:]=[r for r in b['rows'] if r['id']!='liner_corner_2m']
        s=engine.recalculate(s,s['inputs'])
        self.assertFalse(any(r['id']=='liner_corner_2m' for b in s['variants']['economy']['blocks'] for r in b['rows']))

    def test_manual_level_builtin_cycles_do_not_accumulate_quantity_or_money(self):
        s=engine.generate(NEW);row(s,'inlet').update(auto_quantity=False,quantity='4',price='100',unit_price_manual=True)
        for _ in range(3):
            row(s,'skimmer').update(level_status='included',level_evidence='Synthetic model passport')
            s=engine.evaluate(s)
            self.assertEqual(row(s,'inlet')['quantity'],'4.000')
            self.assertEqual(row(s,'inlet')['level_quantity'],'0.000')
            self.assertEqual(filtration_quantity(row(s,'inlet')),3)
            s=engine.evaluate(s)
            self.assertEqual(row(s,'inlet')['level_quantity_credit'],'1')
            row(s,'skimmer')['level_status']='absent'
            s=engine.recalculate(s,s['inputs'])
            self.assertEqual(row(s,'inlet')['quantity'],'4.000')
            self.assertEqual(row(s,'inlet')['level_quantity'],'1.000')
            self.assertEqual(row(s,'inlet')['amount'],'400.00')
        row(s,'skimmer')['level_status']='included';s=engine.evaluate(s)
        row(s,'inlet')['quantity']='3';s=engine.evaluate(s)
        row(s,'skimmer')['level_status']='absent';s=engine.evaluate(s)
        self.assertEqual(row(s,'inlet')['quantity'],'4.000')
