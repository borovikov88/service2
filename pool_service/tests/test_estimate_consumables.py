from copy import deepcopy
from decimal import Decimal
import json
from django.test import SimpleTestCase
from pool_service.services import pool_estimates as engine, estimate_consumables as consumables, estimate_layout as layout
from .test_estimate_construction import NEW


def row(s, tier='economy'):
    return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['id']==consumables.ID)


class ConsumablesTests(SimpleTestCase):
    def test_floor_walls_no_stock_or_band_and_same_final_price_three_quotes(self):
        s=engine.generate({**NEW,'bowl_height':'1.7'})
        # 32 + 24*1.7 = 72.8; 25480 rounds up once to 26000.
        for tier in s['variants']:
            r=row(s,tier)
            self.assertEqual((r['unit'],r['quantity'],r['price'],r['amount']),('компл.','1.000','26000.00','26000.00'))
            self.assertIn('72.8',r['construction_quantity_note'])
        self.assertEqual(consumables.values(s)[0],Decimal('72.8'))
        s['inputs']['tech_top_finish']='film_band'
        s['inputs']['tech_top_width']='1'
        self.assertEqual(consumables.values(s),(Decimal('72.8'),'26000.00'))

    def test_thousand_boundary_rounds_only_sum(self):
        for height,expected in [('1','2000.00'),('1.178571','2000.00'),('1.178572','3000.00')]:
            s={'inputs':{'length':'1','width':'1','bowl_height':height}}
            self.assertEqual(consumables.values(s)[1],expected)

    def test_dimensions_roundtrip_and_total_delta_only_allowance(self):
        s=engine.generate({**NEW,'bowl_height':'1.7'})
        old=deepcopy(s);old.pop('consumables_version')
        for v in old['variants'].values():
            for b in v['blocks']:b['rows']=[r for r in b['rows'] if r['id']!=consumables.ID]
        original=engine.evaluate(old)
        for tier in s['variants']:
            self.assertEqual(Decimal(s['variants'][tier]['total'])-Decimal(original['variants'][tier]['total']),26000)
        changed=engine.recalculate(s,{**s['inputs'],'width':'5'})
        self.assertEqual(row(changed)['price'],'30000.00')
        self.assertEqual(engine.evaluate(json.loads(json.dumps(changed)))['variants'],changed['variants'])
        self.assertEqual(original,engine.evaluate(old))
        self.assertFalse(any(r['id']==consumables.ID for b in original['variants']['economy']['blocks'] for r in b['rows']))
        upgraded=engine.recalculate(original,original['inputs'])
        self.assertEqual(row(upgraded)['price'],'26000.00')

    def test_manual_clear_zero_custom_quantity_hidden_and_deletion(self):
        for value in (None,'0.00','123.45'):
            s=engine.generate(NEW);row(s).update(price=value,quantity='2',excluded=True,reason='Пользователь')
            s=engine.recalculate(engine.evaluate(s),{**s['inputs'],'width':'5'})
            self.assertEqual(row(s)['price'],value)
            self.assertEqual(row(s)['quantity'],'2.000')
            self.assertTrue(row(s)['excluded'])
            self.assertIsNone(row(s)['amount'])
        s=engine.generate(NEW)
        for b in s['variants']['economy']['blocks']:b['rows']=[r for r in b['rows'] if r['id']!=consumables.ID]
        for _ in range(2):s=engine.recalculate(s,s['inputs'])
        self.assertFalse(any(r['id']==consumables.ID for b in s['variants']['economy']['blocks'] for r in b['rows']))

    def test_wrong_unit_suspension_restores_without_manual_and_no_duplicate(self):
        s=engine.generate(NEW);row(s)['unit']='м²'
        s=engine.evaluate(s);self.assertIsNone(row(s)['price'])
        row(s)['unit']='компл.';s=engine.evaluate(s)
        self.assertFalse(row(s)['unit_price_manual'])
        self.assertEqual(row(s)['price'],'26000.00')
        self.assertEqual(sum(r['id']==consumables.ID for b in s['variants']['economy']['blocks'] for r in b['rows']),1)

    def test_explicit_catalog_replacement_keeps_price_refresh_semantics(self):
        from .test_estimate_catalog import fixture
        from pool_service.services.estimate_catalog import apply_product, refresh_prices
        data=fixture();s=engine.generate(NEW)
        row(s).update(price='0',unit_price_manual=True)
        apply_product(row(s),data['products']['1'],data,'manual')
        s=engine.recalculate(s,{**s['inputs'],'width':'5'})
        self.assertFalse(row(s)['unit_price_manual'])
        self.assertEqual(row(s)['price'],'100.10')
        for price in ('1200.00','1300.00'):
            data['products']['1']['price']=price
            s,updated,_=refresh_prices(s,data,'economy')
            self.assertGreaterEqual(updated,1)
            self.assertEqual(row(s)['price'],price)
            self.assertFalse(row(s)['unit_price_manual'])

    def test_both_captions_follow_active_construction_rows_for_editor_and_print(self):
        s=engine.generate(NEW);v=s['variants']['economy']
        for b in v['blocks']:
            if b['id'] in {'construction','construction_materials'}:
                for r in b['rows']:r['excluded']=True
        for client in (False,True):self.assertFalse(any(s['show_title'] for s in layout.sections(v,client=client)))
        b=next(b for b in v['blocks'] if b['id']=='construction_materials');b['rows'][0]['excluded']=False
        for client in (False,True):self.assertEqual([s['show_title'] for s in layout.sections(v,client=client)],[True,True])
        b['excluded']=True
        self.assertFalse(any(s['show_title'] for s in layout.sections(v)))
