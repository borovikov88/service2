from copy import deepcopy
from decimal import Decimal as D
from django.test import SimpleTestCase
from pool_service.services.pool_estimates import generate, evaluate, recalculate, EstimateInputError
from pool_service.services import estimate_construction as package
from .test_pool_estimates import INPUTS, block

NEW={**INPUTS,'construction_scheme':'package','finish':'film'}
def row(s,rid,tier='economy'):
    return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['id']==rid)

class ConstructionTests(SimpleTestCase):
    def test_manual_quantity_toggle_preserves_exact_package_quantities_and_amounts(self):
        s = generate(NEW)
        before = deepcopy(s)
        for variant in s['variants'].values():
            for b in variant['blocks']:
                for r in b['rows']:
                    if r['basis'] in package.BASES:
                        r['auto_quantity'] = False
        manual = evaluate(s)
        for tier in s['variants']:
            self.assertEqual(manual['variants'][tier]['total'], before['variants'][tier]['total'])
            for b in manual['variants'][tier]['blocks']:
                for r in b['rows']:
                    if r['basis'] in package.BASES:
                        original = row(before, r['id'], tier)
                        self.assertEqual(r['quantity'], original['quantity'])
                        self.assertEqual(r['amount'], original['amount'])
        self.assertEqual(D(row(manual, 'material_sand')['quantity']), D('5.9136'))
        self.assertEqual(evaluate(manual)['variants'], manual['variants'])
        row(manual, 'material_sand')['quantity'] = '5.914'
        self.assertEqual(row(evaluate(manual), 'material_sand')['quantity'], '5.914')
        legacy = generate(INPUTS)
        old = row(legacy, 'pump')
        old.update(quantity='1.234', auto_quantity=False)
        self.assertEqual(row(evaluate(legacy), 'pump')['quantity'], '1.234')

    def test_freeboard_defaults_and_legacy_do_not_add_twice(self):
        s=generate(NEW)
        self.assertEqual(D(s['inputs']['bowl_height']),D('1.65'))
        self.assertEqual(D(s['inputs']['freeboard']),D('.15'))
        changed=recalculate(s,{**s['inputs'],'depth_start':'1.6','depth_end':'1.6'})
        self.assertEqual(D(changed['inputs']['bowl_height']),D('1.75'))
        legacy=generate({**INPUTS,'bowl_height':'1.7'})
        before=deepcopy(legacy)
        self.assertNotIn('freeboard',evaluate(legacy)['inputs'])
        self.assertEqual(legacy,before)
        self.assertEqual(D(recalculate(legacy,legacy['inputs'])['inputs']['bowl_height']),D('1.7'))
        explicit=generate({**NEW,'bowl_height':'1.7'})
        self.assertNotIn('freeboard',explicit['inputs'])
        self.assertEqual(D(explicit['inputs']['bowl_height']),D('1.7'))
        with self.assertRaises(EstimateInputError):generate({**NEW,'depth_end':'1.8'})

    def test_package_exact_bowl_only_no_double_labor_all_quotes(self):
        s=generate(NEW)
        for tier in s['variants']:
            ids={r['id'] for r in block(s,'construction',tier)['rows']}
            self.assertEqual(ids,{r[0] for r in package.WORKS})
            self.assertEqual(row(s,'bowl_package',tier)['amount'],'311520.00')
            self.assertEqual(row(s,'material_concrete_bowl',tier)['amount'],'149529.60')
            self.assertEqual(row(s,'material_concrete_base',tier)['amount'],'51226.56')
            self.assertIsNone(row(s,'excavation_package',tier)['price'])
            self.assertTrue(all(row(s,k,tier)['price'] is None for k in ('delivery_concrete','delivery_rebar','delivery_sand','soil_removal')))
            self.assertNotIn('work_construction_transport',ids)
        self.assertEqual(evaluate(s)['variants'],s['variants'])
        self.assertEqual(recalculate(s,s['inputs'])['variants'],s['variants'])

    def test_prices_markup_once_sand_and_penoplex_and_supplies(self):
        s=generate(NEW)
        self.assertEqual(D(row(s,'material_sand')['quantity']),D('3.696')*D('1.6'))
        self.assertEqual(row(s,'material_sand')['price'],'1140.00')
        self.assertEqual(row(s,'material_supplies')['price'],'17900.00') #71.6*250; no film band/stock/markup
        self.assertEqual(row(s,'material_supplies')['quantity'],'1')
        self.assertEqual(row(s,'material_insulation')['amount'],'27214.75') #39.68*3326.40/4.85
        for rid in ('material_concrete_bowl','material_sand','material_rebar','material_insulation'):
            p=row(s,rid)['construction_price']
            self.assertEqual(D(p['markup']),D(20))
            self.assertNotEqual(p['retail'],row(s,rid)['price'])
        for rid,value in [('material_sand','0'),('material_supplies','100')]:
            row(s,rid).update(price=value,unit_price_manual=True)
        changed=recalculate(s,{**s['inputs'],'length':'9'})
        self.assertEqual(row(changed,'material_sand')['price'],'0.00')
        self.assertEqual(row(changed,'material_supplies')['price'],'100.00')

    def test_rebar_threshold_reserve_once_and_manual_parameters(self):
        a=generate({**NEW,'length':'10','construction_rebar_reserve':'0'})
        b=generate({**NEW,'length':'10','construction_rebar_reserve':'5'})
        self.assertEqual(D(b['construction_assumptions']['rebar_diameter']),10)
        self.assertAlmostEqual(D(row(b,'material_rebar')['quantity']),D(row(a,'material_rebar')['quantity'])*D('1.05'),places=15)
        c=generate({**NEW,'length':'10.001'})
        self.assertEqual(D(c['construction_assumptions']['rebar_diameter']),12)
        self.assertEqual(row(recalculate(b,b['inputs']),'material_rebar')['amount'],row(b,'material_rebar')['amount'])
        manual=generate({**NEW,'construction_diameter_mode':'manual','construction_rebar_diameter':'14'})
        self.assertEqual(D(manual['construction_assumptions']['rebar_diameter']),14)
        self.assertTrue(any('конструктором' in w for w in b['variants']['economy']['construction_notes']))

    def test_unknown_zero_export_and_manual_handling_scope(self):
        s=generate(NEW)
        self.assertIsNone(row(s,'handling_package')['quantity'])
        expected=D(s['construction_assumptions']['pit_volume'])-D(s['geometry']['tech_backfill_volume'])
        self.assertEqual(D(row(s,'soil_removal')['quantity']),expected)
        unknown=recalculate(s,{**s['inputs'],'construction_soil_reuse':'unknown','freeboard':''})
        self.assertIsNone(row(unknown,'soil_removal')['quantity'])
        self.assertIsNone(row(unknown,'bowl_package')['quantity'])
        self.assertIsNone(row(unknown,'material_rebar')['quantity'])
        row(s,'material_sand').update(handling_mode='manual')
        row(s,'material_concrete_bowl').update(handling_mode='manual',handling_mass='100')
        handled=evaluate(s)
        self.assertEqual(D(row(handled,'handling_package')['quantity']),D(row(s,'material_sand')['quantity']))
        self.assertEqual(row(handled,'handling_package')['amount'],'1774.08')
        row(s,'material_sand').update(excluded=True)
        self.assertIsNone(row(evaluate(s),'handling_package')['quantity'])
        row(s,'handling_package').update(quantity='0',auto_quantity=False)
        self.assertEqual(row(evaluate(s),'handling_package')['amount'],'0.00')

    def test_changed_units_do_not_reuse_automatic_quantity_price_or_handling_mass(self):
        s=generate(NEW)
        row(s,'material_sand').update(unit='кг',handling_mode='manual')
        broken=evaluate(s)
        self.assertIsNone(row(broken,'material_sand')['quantity'])
        self.assertIsNone(row(broken,'material_sand')['amount'])
        self.assertIsNone(row(broken,'handling_package')['quantity'])
        row(s,'material_sand').update(quantity='100',auto_quantity=False)
        self.assertIsNone(row(evaluate(s),'material_sand')['amount'])
        row(s,'material_sand').update(price='1.14',unit_price_manual=True)
        corrected=evaluate(s)
        self.assertEqual(row(corrected,'material_sand')['amount'],'114.00')
        self.assertEqual(row(corrected,'handling_package')['amount'],'30.00')
        row(s,'bowl_package').update(unit='компл.')
        self.assertIsNone(row(evaluate(s),'bowl_package')['amount'])

    def test_unknown_penoplex_thickness_does_not_charge_50mm_price(self):
        s=generate({**NEW,'tech_insulation_wall_thickness':'.1'})
        self.assertIsNone(row(s,'material_insulation')['price'])
        self.assertIsNone(row(s,'material_insulation')['amount'])
        restored=recalculate(s,{**s['inputs'],'tech_insulation_wall_thickness':'.05'})
        self.assertEqual(row(restored,'material_insulation')['price'],'685.86')
        row(s,'material_insulation').update(price='0',unit_price_manual=True)
        self.assertEqual(row(evaluate(s),'material_insulation')['amount'],'0.00')

    def test_catalog_replacement_clears_suspended_source_price(self):
        from pool_service.services.estimate_catalog import parse_catalog, apply_product
        from .test_estimate_catalog import feed, offer
        s=generate({**NEW,'tech_insulation_wall_thickness':'.1'})
        c=parse_catalog(feed(offer(99,'Утеплитель другой',price='100',description='')))
        apply_product(row(s,'material_insulation'),c['products']['99'],c)
        chosen=evaluate(s)
        self.assertEqual(row(chosen,'material_insulation')['price'],'100.00')
        restored=recalculate(chosen,{**chosen['inputs'],'tech_insulation_wall_thickness':'.05'})
        self.assertEqual(row(restored,'material_insulation')['price'],'100.00')
        self.assertIsNone(row(restored,'material_insulation')['construction_price'])
        apply_product(row(s,'material_supplies'),c['products']['99'],c)
        self.assertEqual(row(recalculate(s,{**s['inputs'],'length':'9'}),'material_supplies')['price'],'100.00')
