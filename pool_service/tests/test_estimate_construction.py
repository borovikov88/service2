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
        self.assertEqual(D(row(manual, 'material_sand')['quantity']), D('6'))
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
            self.assertEqual(row(s,'material_concrete_bowl',tier)['amount'],'153600.00')
            self.assertEqual(row(s,'material_concrete_base',tier)['amount'],'55440.00')
            self.assertIsNone(row(s,'excavation_package',tier)['price'])
            self.assertTrue(all(row(s,k,tier)['price'] is None for k in ('delivery_concrete','delivery_rebar','delivery_sand','soil_removal')))
            self.assertNotIn('work_construction_transport',ids)
        self.assertEqual(evaluate(s)['variants'],s['variants'])
        self.assertEqual(recalculate(s,s['inputs'])['variants'],s['variants'])

    def test_prices_markup_once_sand_and_penoplex_and_supplies(self):
        s=generate(NEW)
        self.assertEqual(D(row(s,'material_sand')['quantity']),D('6'))
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
        self.assertAlmostEqual(D(b['construction_assumptions']['rebar_mass']),D(a['construction_assumptions']['rebar_mass'])*D('1.05'),places=15)
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
        self.assertEqual(row(handled,'handling_package')['amount'],'1800.00')
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

    def test_material_quantization_only_new_automatic_materials(self):
        s=generate(NEW)
        expected={'material_sand':('6','6840.00'), 'material_concrete_bowl':('16','153600.00'),
                  'material_concrete_base':('6','55440.00'), 'material_rebar':('1.5','135000.00')}
        for tier in s['variants']:
            for rid,(quantity,amount) in expected.items():
                r=row(s,rid,tier)
                self.assertEqual(D(r['quantity']),D(quantity))
                self.assertEqual(r['amount'],amount)
                self.assertIn('сметное',r['construction_quantity_note'])
            self.assertEqual(D(row(s,'bowl_package',tier)['quantity']),D('15.576'))
            self.assertEqual(row(s,'bowl_package',tier)['amount'],'311520.00')
        self.assertEqual(D(s['geometry']['concrete_total']),D('15.576'))
        old=deepcopy(s);old.pop('construction_quantity_version')
        for tier in old['variants']:
            row(old,'material_rebar',tier).update(unit='кг',basis='pkg_rebar_mass',price='90.00',
                construction_price={'retail':'75','markup':'20','divisor':'1'})
        old=evaluate(old)
        self.assertEqual(D(row(old,'material_sand')['quantity']),D('5.9136'))
        self.assertEqual(row(old,'material_sand')['amount'],'6741.50')
        self.assertEqual(recalculate(old,old['inputs'])['variants'],old['variants'])
        self.assertNotIn('construction_quantity_version',evaluate(old))
        self.assertEqual(D(s['variants']['economy']['total'])-D(old['variants']['economy']['total']),D('30049.54'))
        for rid in expected:row(s,rid).update(auto_quantity=False,quantity='1.2345')
        manual=evaluate(s)
        self.assertEqual(row(recalculate(manual,manual['inputs']),'material_sand')['quantity'],'1.2345')
        self.assertEqual(row(manual,'material_concrete_bowl')['quantity'],'1.2345')
        for step in (D('1'),D('0.5')):
            for value in (D('0'),step,step+D('.000001'),step-D('.000001')):
                rounded=package.estimate_quantity(value,step)
                self.assertGreaterEqual(rounded,value)
                self.assertLess(rounded-value,step)
                self.assertEqual(package.estimate_quantity(rounded,step),rounded)
            self.assertIsNone(package.estimate_quantity(None,step))

    def test_rebar_tonnes_boundaries_after_reserve_and_handling(self):
        from unittest.mock import patch
        s=generate(NEW)
        original=package.metrics(s['inputs'],s['geometry'])
        for kg, expected in [('0','0'),('500','0.5'),('500.000001','1'),
                             ('1000','1'),('1000.000001','1.5'),('1500','1.5'),('1500.000001','2')]:
            facts={**original,'rebar_mass':D(kg),'rebar_tonnes':D(kg)/1000}
            with patch.object(package,'metrics',return_value=facts):
                changed=deepcopy(s)
                row(changed,'material_rebar')['handling_mode']='manual'
                result=evaluate(changed)
                r=row(result,'material_rebar')
                self.assertEqual(r['unit'],'т')
                self.assertEqual(D(r['quantity']),D(expected))
                self.assertEqual(D(r['amount']),D(expected)*90000)
                self.assertEqual(D(row(result,'handling_package')['quantity']),D(expected))
                self.assertEqual(D(row(result,'handling_package')['amount']),D(expected)*300)
                self.assertEqual(row(result,'bowl_package')['quantity'],row(s,'bowl_package')['quantity'])

    def test_rebar_v2_exact_mass_reserve_once_and_noop_json_roundtrip(self):
        import json
        s=generate(NEW); r=row(s,'material_rebar')
        self.assertEqual(s['construction_quantity_version'],2)
        self.assertEqual((r['unit'],r['basis'],r['price'],r['amount']),('т','pkg_rebar_tonnes','90000.00','135000.00'))
        self.assertEqual(D(r['construction_price']['retail']),D('75000'))
        self.assertEqual(D(r['construction_calculated_quantity']),D(s['construction_assumptions']['rebar_mass'])/1000)
        self.assertIn('до 0.5 т',r['construction_quantity_note'])
        loaded=evaluate(json.loads(json.dumps(s)))
        again=recalculate(loaded,loaded['inputs'])
        self.assertEqual(again['variants'],s['variants'])
        self.assertEqual(D(s['construction_assumptions']['rebar_mass']),D(s['construction_assumptions']['rebar_base_mass'])*D('1.05'))

    def test_historical_rebar_kg_versions_do_not_convert(self):
        for version in (None,1):
            s=generate(NEW)
            if version is None:s.pop('construction_quantity_version')
            else:s['construction_quantity_version']=version
            for tier in s['variants']:
                row(s,'material_rebar',tier).update(unit='кг',basis='pkg_rebar_mass',price='90.00',
                    construction_price={'retail':'75','markup':'20','divisor':'1'})
            old=evaluate(s); again=recalculate(old,old['inputs'])
            self.assertEqual(row(again,'material_rebar'),row(old,'material_rebar'))
            self.assertEqual(row(again,'material_rebar')['unit'],'кг')
            self.assertEqual(again.get('construction_quantity_version'),version)
            if version==1:self.assertEqual(row(again,'material_rebar')['amount'],'113355.00')
            else:self.assertEqual(row(again,'material_rebar')['amount'],'113332.80')
            row(old,'material_rebar').update(auto_quantity=False,quantity='1234.5678',price='88.88',unit_price_manual=True)
            manual=recalculate(old,old['inputs'])
            self.assertEqual(row(manual,'material_rebar')['quantity'],'1234.5678')
            self.assertEqual(row(manual,'material_rebar')['price'],'88.88')
            self.assertEqual(row(manual,'material_rebar')['unit'],'кг')

    def test_manual_tonnes_unit_switch_and_explicit_handling_mass(self):
        s=generate(NEW)
        row(s,'material_rebar').update(quantity='1.234567',auto_quantity=False,price='80000',unit_price_manual=True,handling_mode='manual')
        manual=recalculate(s,s['inputs'])
        self.assertEqual(row(manual,'material_rebar')['quantity'],'1.234567')
        self.assertEqual(row(manual,'material_rebar')['amount'],'98765.36')
        self.assertEqual(D(row(manual,'handling_package')['quantity']),D('1.234567'))
        row(s,'material_rebar')['handling_mass']='2.25'
        self.assertEqual(D(row(evaluate(s),'handling_package')['quantity']),D('2.25'))
        switched=generate(NEW);row(switched,'material_rebar').update(unit='кг',handling_mode='manual')
        invalid=evaluate(switched)
        self.assertIsNone(row(invalid,'material_rebar')['amount'])
        self.assertIsNone(row(invalid,'handling_package')['quantity'])
        row(switched,'material_rebar').update(auto_quantity=False,quantity='1500',price='90',unit_price_manual=True)
        fixed=evaluate(switched)
        self.assertEqual(row(fixed,'material_rebar')['amount'],'135000.00')
        self.assertEqual(D(row(fixed,'handling_package')['quantity']),D('1.5'))
