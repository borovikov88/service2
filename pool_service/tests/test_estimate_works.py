"""Work catalogue, incomplete quantities, reversible scope and legacy upgrades."""
from copy import deepcopy
from decimal import Decimal
from django.test import SimpleTestCase
from pool_service.services import estimate_works as works
from pool_service.services.pool_estimates import generate, evaluate, recalculate, make_row, EstimateInputError
from pool_service.tests.test_pool_estimates import INPUTS, block


def work(s, code, variant='economy'):
    return next(r for b in s['variants'][variant]['blocks'] for r in b['rows'] if r.get('work_code') == code)


class EstimateWorkTests(SimpleTestCase):
    def test_all_operations_with_rates_units_and_no_old_project_quantities(self):
        s = generate({**INPUTS, 'finish': 'film'})
        expected = {x[0] for x in works.WORKS} | {'embedded_work', 'equipment_work', 'finish_film', 'underlay_film', 'liner_steps'}
        for key, variant in s['variants'].items():
            rows = [r for b in variant['blocks'] for r in b['rows'] if r['kind'] == 'work']
            self.assertEqual({r['work_code'] for r in rows}, expected)
            self.assertEqual(len(rows), 28)
            self.assertEqual(work(s, 'excavation', key)['quantity'], None)
            self.assertIsNone(work(s, 'embedded_work', key)['price'])  # Formula depends on the old fitting set.
        self.assertEqual(work(s, 'equipment_work')['price'], '70000.00')
        self.assertEqual(work(s, 'equipment_work', 'standard')['price'], '70000.00')
        self.assertEqual(work(s, 'equipment_work', 'premium')['price'], '70000.00')
        self.assertEqual(work(s, 'sand_base')['price'], '200.00')
        self.assertEqual(work(s, 'sand_base', 'premium')['price'], '200.00')
        self.assertEqual(work(s, 'finish_transport')['unit'], 'рейс')
        self.assertEqual(work(s, 'finish_transport', 'standard')['unit'], 'компл.')
        self.assertIsNone(work(s, 'finish_transport')['quantity'])

    def test_missing_quantity_never_contributes_zero_or_guessed_one(self):
        s = generate(INPUTS)
        row = work(s, 'excavation'); row['price'] = '10.05'
        base = Decimal(s['variants']['economy']['total'])
        result = evaluate(s)
        self.assertIsNone(work(result, 'excavation')['amount'])
        self.assertGreater(result['variants']['economy']['missing_quantities'], 0)
        row.update(quantity='3', auto_quantity=False)
        self.assertEqual(Decimal(evaluate(s)['variants']['economy']['total']) - base, Decimal('30.15'))
        row.update(excluded=True)
        self.assertEqual(Decimal(evaluate(s)['variants']['economy']['total']), base)
        block(s)['rows'][0]['quantity'] = None
        with self.assertRaises(EstimateInputError): evaluate(s)

    def test_labor_has_no_stock_and_optional_rows_stay_excluded(self):
        s = generate({**INPUTS, 'finish': 'film', 'length': '8.123'})
        for code in ('finish_film', 'underlay_film', 'primer'):
            self.assertEqual(work(s, code)['quantity'], '69.000')
        self.assertIsNone(work(s, 'plaster')['quantity'])
        self.assertIsNone(work(s, 'plaster')['amount'])
        for code in ('liner_steps', 'gutters', 'countercurrent', 'waterfall', 'construction_travel'):
            self.assertTrue(work(s, code)['excluded'])
            self.assertIsNone(work(s, code)['amount'])
        for code in ('finish_travel','accommodation'):
            self.assertFalse(work(s,code)['excluded'])
            self.assertIsNone(work(s,code)['quantity'])
            self.assertIsNone(work(s,code)['price'])
        work(s, 'primer').update(auto_quantity=False, quantity='7', price='11')
        result = recalculate(s, {**INPUTS, 'finish': 'tile', 'length': '10'})
        self.assertTrue(work(result, 'underlay_film')['excluded'])
        self.assertEqual(work(result, 'primer')['quantity'], '7.000')
        self.assertEqual(work(result, 'primer')['price'], '11.00')
        self.assertIsNone(work(result, 'waterproofing_tile')['price'])
        restored = recalculate(result, {**INPUTS, 'finish': 'film'})
        self.assertFalse(work(restored, 'underlay_film')['excluded'])
        self.assertTrue(work(restored, 'liner_steps')['excluded'])

    def test_explicit_legacy_upgrade_preserves_total_scope_and_rates(self):
        from pool_service.services.estimate_technology import MODES
        s = generate({**INPUTS, **{'tech_'+key:'' for key in MODES}})
        for v in s['variants'].values():
            for b in v['blocks']:
                b['rows'] = [r for r in b['rows'] if not r['id'].startswith('work_')]
            next(b for b in v['blocks'] if b['id']=='construction')['rows'].append(make_row('bowl_work', 'Общая работа'))
        block(s, 'construction')['rows'][-1]['price'] = '123'
        before = deepcopy(s)
        result = evaluate(works.add_works(s))
        self.assertEqual(work(result, 'bowl_work')['price'], '123.00')
        self.assertTrue(all(r['excluded'] for r in block(result, 'construction')['rows'] if r['id'].startswith('work_')))
        self.assertEqual(work(result, 'equipment_work')['price'], work(before, 'equipment_work')['price'])
        again = evaluate(works.add_works(result))
        self.assertEqual(again, result)
        self.assertEqual(len(block(before, 'construction')['rows']), 2)

    def test_recalculating_legacy_adds_default_work_only_to_editable_copy(self):
        s = generate({**INPUTS, 'finish': 'film'})
        s.pop('works_catalog')
        for v in s['variants'].values():
            for b in v['blocks']:
                b['rows'] = [r for r in b['rows'] if not r['id'].startswith('work_')]
        before = evaluate(s)
        result = recalculate(before, {**INPUTS, 'finish': 'film'})
        self.assertEqual(Decimal(result['variants']['economy']['total'])-Decimal(before['variants']['economy']['total']),Decimal('18360'))
        self.assertIsNone(work(result,'plaster')['quantity'])
        self.assertEqual(work(result,'underlay_film')['price'],'200.00')
        self.assertFalse(any(r['id'].startswith('work_') for r in block(before,'finish')['rows']))
        again=recalculate(result,result['inputs'])
        self.assertEqual(again['variants'],result['variants'])
        tile = recalculate(before, {**INPUTS, 'finish': 'tile'})
        self.assertTrue(any(r.get('work_code') == 'waterproofing_tile' and r['price'] is None for r in block(tile,'finish')['rows']))

    def test_mosaic_rates_follow_the_workbook_lookup_and_tile_stays_unknown(self):
        mosaic = generate({**INPUTS, 'finish': 'mosaic'})
        self.assertEqual(work(mosaic, 'finish_mosaic', 'standard')['price'], '2500.00')
        self.assertEqual(work(mosaic, 'waterproofing_mosaic', 'premium')['price'], '600.00')
        self.assertEqual(work(mosaic, 'finish_mosaic')['price'], '2000.00')
        self.assertEqual(work(mosaic, 'waterproofing_mosaic')['price'], '200.00')
        tile = recalculate(mosaic, {**INPUTS, 'finish': 'tile'})
        self.assertIsNone(work(tile, 'finish_tile')['price'])
        self.assertIsNone(work(tile, 'waterproofing_tile')['price'])

    def test_automatic_defaults_preserve_manual_discount_clear_and_unknown_rate(self):
        s=generate({**INPUTS,'finish':'film'})
        work(s,'finish_film')['price']='1234'
        work(s,'underlay_film').update(price=None,rate_source='Ручная цена текущей сметы')
        work(s,'primer').update(price=None,selection_locked=True)
        work(s,'sand_base')['price']=None
        result=recalculate(s,s['inputs'])
        self.assertEqual(work(result,'finish_film')['price'],'1234.00')
        self.assertIsNone(work(result,'underlay_film')['price'])
        self.assertIsNone(work(result,'primer')['price'])
        self.assertEqual(work(result,'sand_base')['price'],'200.00')
        self.assertIsNone(work(result,'excavation')['price'])
