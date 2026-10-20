from copy import deepcopy
from decimal import Decimal
from django.test import SimpleTestCase
from pool_service.services.pool_estimates import generate, evaluate, recalculate, EstimateInputError
from pool_service.services import estimate_technology as tech
from pool_service.tests.test_pool_estimates import INPUTS

BASE = {**INPUTS, 'length':'8', 'width':'4', 'depth_start':'1.5', 'depth_end':'1.5',
        'bowl_height':'1.7', 'wall_thickness':'.2', 'slab_thickness':'.2'}
TECH = {
    'tech_sand':'on','tech_sand_thickness':'.1','tech_sand_projection':'.1',
    'tech_preparation':'on','tech_preparation_thickness':'.05','tech_preparation_projection':'0',
    'tech_formwork':'ground_two_sided','tech_insulation_floor':'off','tech_insulation_wall':'off',
    'tech_outer_finish':'off','tech_top_finish':'on','tech_plaster':'walls','tech_plaster_thickness':'.02',
    'tech_pit':'vertical','tech_pit_gap':'.5','tech_bowl_above_ground':'.2','tech_backfill':'vertical',
}

def rows(s, tier='economy'):
    return [r for b in s['variants'][tier]['blocks'] for r in b['rows']]

def row(s, identity, tier='economy'):
    return next(r for r in rows(s,tier) if r['id']==identity or r.get('work_code')==identity)


class TechnologyTests(SimpleTestCase):
    def test_explicit_geometry_and_financial_delta_all_quotes(self):
        old=generate(BASE); new=generate({**BASE,**TECH})
        expected={'tech_sand_volume':'3.956000000','tech_preparation_volume':'1.848000000',
                  'tech_formwork_area':'89.440000','tech_plaster_volume':'0.816000000',
                  'tech_pit_volume':'93.906000000','tech_backfill_volume':'25.270000000'}
        for key,value in expected.items(): self.assertEqual(Decimal(new['geometry'][key]),Decimal(value))
        for tier in new['variants']:
            for code,qty in [('sand_base','40.000'),('base_concrete','2.000'),('formwork','90.000'),
                             ('strip_formwork','90.000'),('plaster','41.000'),('excavation','94.000'),('backfill','26.000')]:
                self.assertEqual(row(new,code,tier)['quantity'],qty)
                self.assertEqual(len([r for r in rows(new,tier) if r.get('work_code')==code]),1)
            self.assertEqual(Decimal(new['variants'][tier]['total'])-Decimal(old['variants'][tier]['total']),Decimal('113500'))
            self.assertIsNone(row(new,'tech_plaster_material',tier)['price'])
            self.assertIsNone(row(new,'bowl_rebar',tier)['quantity'])
        twice=recalculate(new,new['inputs'])
        self.assertEqual([v['total'] for v in new['variants'].values()],[v['total'] for v in twice['variants'].values()])

    def test_unknown_zero_and_off_are_distinct_and_no_hidden_layers(self):
        s=generate({**BASE,**TECH,'tech_sand_projection':''})
        self.assertIsNone(row(s,'sand_base')['quantity'])
        self.assertIsNone(row(s,'excavation')['quantity'])
        self.assertIsNone(row(s,'backfill')['quantity'])
        s=recalculate(s,{**s['inputs'],'tech_sand':'off'})
        self.assertTrue(row(s,'sand_base')['excluded'])
        self.assertEqual(s['geometry']['tech_pit_volume'],'88.830000000')
        for field in ('tech_insulation_wall','tech_outer_finish'):
            s=generate({**BASE,**TECH,field:''})
            self.assertIsNone(row(s,'excavation')['quantity'])
        s=generate({**BASE,**TECH,'tech_pit_gap':'0'})
        self.assertIsNone(row(s,'backfill')['quantity']) # sand projects beyond pit

    def test_insulation_and_finish_explicit_surfaces_no_stock_or_double_work(self):
        s=generate({**BASE,'tech_insulation_floor':'on','tech_insulation_floor_thickness':'.1',
                    'tech_insulation_wall':'on','tech_insulation_wall_thickness':'.05','tech_insulation_wall_height':'1.7',
                    'tech_outer_finish':'on','tech_outer_finish_height':'1.7','tech_top_finish':'on'})
        self.assertEqual(row(s,'insulation')['quantity'],'81.000') # ceil(36.96+43.52)
        self.assertEqual(row(s,'tech_insulation_floor')['quantity'],'37.000')
        self.assertEqual(row(s,'tech_insulation_wall')['quantity'],'44.000')
        self.assertEqual(row(s,'outer_finish')['quantity'],'44.000')
        self.assertEqual(row(s,'top_finish')['quantity'],'5.000')
        self.assertEqual(len([r for r in rows(s) if r.get('work_code')=='insulation']),1)
        s=recalculate(s,{**s['inputs'],'tech_insulation_floor_thickness':''})
        self.assertIsNone(row(s,'insulation')['quantity'])
        self.assertIsNone(row(s,'tech_insulation_floor')['quantity'])

    def test_manual_quantities_prices_units_exclusions_history_preserved(self):
        s=generate({**BASE,**TECH})
        target=row(s,'sand_base'); target.update(auto_quantity=False,quantity='0',price='777',excluded=True,reason='Исключено пользователем')
        target=row(s,'base_concrete'); target.update(auto_quantity=False,quantity='7',unit='компл.',price='888')
        result=recalculate(s,{**s['inputs'],'length':'9','tech_sand_thickness':'.2'})
        self.assertEqual((row(result,'sand_base')['quantity'],row(result,'sand_base')['price']),('0.000','777.00'))
        self.assertTrue(row(result,'sand_base')['quantity_stale'])
        self.assertTrue(row(result,'sand_base')['excluded'])
        self.assertEqual(row(result,'base_concrete')['unit'],'компл.')
        self.assertEqual(row(result,'base_concrete')['quantity'],'7.000')
        # A changed unit must not create a second charge for the same existing work.
        self.assertEqual(len([r for r in rows(result) if r.get('work_code')=='base_concrete']),1)
        history=generate(BASE); saved=deepcopy(history)
        self.assertEqual(evaluate(history)['variants'],saved['variants'])
        self.assertFalse(any(r['id'].startswith('tech_') for r in rows(history)))

    def test_turn_off_restore_and_missing_dimensions(self):
        s=generate({**BASE,**TECH})
        off=recalculate(s,{**s['inputs'],'tech_formwork':'off'})
        self.assertTrue(row(off,'formwork')['excluded'])
        on=recalculate(off,{**off['inputs'],'tech_formwork':'ground_two_sided'})
        self.assertFalse(row(on,'formwork')['excluded'])
        unknown=recalculate(on,{**on['inputs'],'bowl_height':''})
        self.assertIsNone(row(unknown,'formwork')['quantity'])
        for field in ('tech_outer_finish_height','tech_insulation_wall_height','tech_bowl_above_ground'):
            with self.assertRaises(EstimateInputError): generate({**BASE,field:'2'})

    def test_chosen_scheme_cleared_becomes_unknown_without_stale_charges(self):
        s=generate({**BASE,**TECH})
        row(s,'tech_sand_material')['price']='100'
        for feature,code in (('sand','sand_base'),('plaster','plaster')):
            cleared=recalculate(s,{**s['inputs'],'tech_'+feature:''})
            self.assertIsNone(row(cleared,code)['quantity'])
            self.assertIsNone(row(cleared,code)['amount'])
            self.assertIsNone(row(cleared,'tech_'+feature+'_material')['quantity'])
        insulated=generate({**BASE,'tech_insulation_floor':'on','tech_insulation_floor_thickness':'.1','tech_insulation_wall':'off'})
        cleared=recalculate(insulated,{**insulated['inputs'],'tech_insulation_floor':''})
        self.assertIsNone(row(cleared,'insulation')['quantity'])
        self.assertIsNone(row(cleared,'insulation')['amount'])

    def test_fresh_unknown_never_charges_technology_and_transitions_are_consistent(self):
        for initial in ({}, {'tech_plaster':''}, {'tech_plaster':'off'}):
            snapshot=generate({**BASE,**initial})
            self.assertEqual(snapshot['technology_version'],1)
            for tier in snapshot['variants']:
                for _,_,_,_,unit,_,code in tech.ROWS:
                    if code and any(r.get('work_code')==code for r in rows(snapshot,tier)):
                        self.assertIsNone(row(snapshot,code,tier)['quantity'])
                        self.assertIsNone(row(snapshot,code,tier)['amount'])
            empty=recalculate(snapshot,{**snapshot['inputs'],'tech_plaster':''})
            for tier in empty['variants']:
                plaster=row(empty,'plaster',tier)
                self.assertIsNone(plaster['quantity'])
                self.assertIsNone(plaster['amount'])
                self.assertFalse(plaster['excluded'])
                self.assertEqual(plaster['basis'],'tech_plaster_area')
            self.assertEqual([v['total'] for v in empty['variants'].values()],
                             [v['total'] for v in generate(BASE)['variants'].values()])

    def test_unmarked_history_retains_legacy_plaster_and_manual_changes(self):
        historical=generate(BASE)
        historical.pop('technology_version')
        for tier in historical['variants']:
            for r in rows(historical,tier):
                if r['basis'] in tech.BASES:
                    r.update(basis='',auto_quantity=False)
            row(historical,'plaster',tier).update(basis='finish_net',auto_quantity=True,quantity='73.000')
        frozen=evaluate(historical)
        self.assertNotIn('technology_version',frozen)
        for result in (evaluate(frozen),recalculate(frozen,frozen['inputs'])):
            for tier in result['variants']:
                self.assertEqual(row(result,'plaster',tier)['quantity'],'73.000')
                self.assertEqual(row(result,'plaster',tier)['amount'],'65700.00')
                self.assertEqual(result['variants'][tier]['total'],frozen['variants'][tier]['total'])
        for tier in historical['variants']:
            row(historical,'plaster',tier).update(quantity='12',auto_quantity=False,price='1234')
        manual=recalculate(historical,{**historical['inputs'],'tech_plaster':'walls'})
        self.assertEqual(row(manual,'plaster')['quantity'],'12.000')
        self.assertEqual(row(manual,'plaster')['price'],'1234.00')
