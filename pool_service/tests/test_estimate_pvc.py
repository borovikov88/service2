from copy import deepcopy
from decimal import Decimal
import json
from django.test import SimpleTestCase
from pool_service.services.pool_estimates import generate, evaluate, recalculate
from .test_pool_estimates import INPUTS, block


def row(s, identity='piping', tier='economy'):
    return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['id']==identity)


class PvcAreaTests(SimpleTestCase):
    def test_old_rates_stay_frozen_until_explicit_recalculation(self):
        s=generate(INPUTS);s.pop('pvc_rate_version')
        for tier in s['variants']:
            for rid,value in [('pvc_embedded','32000.00'),('piping','38400.00')]:
                row(s,rid,tier).update(price=value,pvc_computed_price=value)
        old=evaluate(s);before=deepcopy(old)
        self.assertEqual(row(old)['price'],'38400.00')
        upgraded=recalculate(old,old['inputs'])
        for tier in upgraded['variants']:
            self.assertEqual(row(upgraded,'pvc_embedded',tier)['price'],'48000.00')
            self.assertEqual(row(upgraded,'piping',tier)['price'],'57600.00')
            self.assertEqual(Decimal(upgraded['variants'][tier]['total'])-Decimal(old['variants'][tier]['total']),Decimal('35200'))
        self.assertEqual(old,before)
        self.assertEqual(evaluate(upgraded)['variants'],upgraded['variants'])

    def test_same_final_rates_three_quotes_and_no_duplicate_old_allowance(self):
        s=generate(INPUTS)
        for tier in s['variants']:
            for identity,amount in [('pvc_embedded','48000.00'),('piping','57600.00')]:
                r=row(s,identity,tier)
                self.assertEqual(r['quantity'],'1.000')
                self.assertEqual(r['price'],amount)
                self.assertEqual(r['amount'],amount)
                self.assertEqual(r['unit'],'компл.')
                self.assertEqual(r['kind'],'other')
                self.assertIn('без работ и дополнительных 20%',r['construction_quantity_note'])
            self.assertEqual(sum(r['id']=='piping' for b in s['variants'][tier]['blocks'] for r in b['rows']),1)
        old=deepcopy(s);old.pop('pvc_area_version')
        for v in old['variants'].values():
            for b in v['blocks']:
                b['rows']=[r for r in b['rows'] if r['id']!='pvc_embedded']
                for r in b['rows']:
                    if r['id']=='piping':r.update(price=None,pvc_computed_price=None)
        legacy=evaluate(old)
        for tier in s['variants']:
            self.assertEqual(Decimal(s['variants'][tier]['total'])-Decimal(legacy['variants'][tier]['total']),Decimal('105600'))
        legacy=recalculate(legacy,{**legacy['inputs'],'length':'9'})
        self.assertIsNone(row(legacy)['price'])
        self.assertFalse(any(r['id']=='pvc_embedded' for b in legacy['variants']['economy']['blocks'] for r in b['rows']))

    def test_dimensions_and_roundtrip_recompute_price_not_quantity(self):
        s=generate(INPUTS)
        changed=recalculate(s,{**s['inputs'],'length':'9'})
        self.assertEqual(row(changed)['price'],'64800.00')
        self.assertEqual(row(changed,'pvc_embedded')['price'],'54000.00')
        again=evaluate(json.loads(json.dumps(changed)))
        self.assertEqual(again['variants'],changed['variants'])
        self.assertEqual(row(again)['quantity'],'1.000')

    def test_manual_zero_clear_and_custom_prices_survive(self):
        for value in ['0.00',None,'123.45']:
            s=generate(INPUTS);row(s)['price']=value
            saved=evaluate(s)
            self.assertTrue(row(saved)['unit_price_manual'])
            again=recalculate(saved,{**saved['inputs'],'width':'5'})
            self.assertEqual(row(again)['price'],value)
            self.assertEqual(row(again)['amount'],value)
            self.assertEqual(row(again,'pvc_embedded')['price'],'60000.00')
            self.assertEqual(row(again,'piping','standard')['price'],'72000.00')

    def test_explicit_manual_same_price_quantity_zero_and_existing(self):
        s=generate(INPUTS);row(s).update(unit_price_manual=True,quantity='0',auto_quantity=False)
        changed=recalculate(s,{**s['inputs'],'length':'9'})
        self.assertEqual(row(changed)['price'],'57600.00')
        self.assertEqual(row(changed)['amount'],'0.00')
        row(changed).update(existing=True,quantity='1')
        again=evaluate(changed)
        self.assertIsNone(row(again)['amount'])

    def test_hidden_deleted_and_order_stay_preserved(self):
        s=generate(INPUTS);row(s).update(excluded=True,reason='Пользователь')
        b=block(s);b['rows'].reverse();ids=[r['id'] for r in b['rows']]
        embedded=block(s,'embedded');embedded['rows']=[r for r in embedded['rows'] if r['id']!='pvc_embedded']
        result=recalculate(s,{**s['inputs'],'length':'9'})
        self.assertEqual([r['id'] for r in block(result)['rows']],ids)
        self.assertTrue(row(result)['excluded'])
        self.assertIsNone(row(result)['amount'])
        self.assertEqual(row(result)['reason'],'Пользователь')
        self.assertFalse(any(r['id']=='pvc_embedded' for r in block(result,'embedded')['rows']))
        self.assertEqual(row(result)['price'],'64800.00')
        row(result)['excluded']=False
        self.assertEqual(row(evaluate(result))['amount'],'64800.00')

    def test_unit_mismatch_suspends_auto_without_turning_it_into_manual_clear(self):
        s=generate(INPUTS);row(s)['unit']='м'
        suspended=evaluate(s)
        self.assertIsNone(row(suspended)['price'])
        self.assertTrue(row(suspended)['pvc_price_suspended'])
        row(suspended)['unit']='компл.'
        restored=evaluate(json.loads(json.dumps(suspended)))
        self.assertEqual(row(restored)['price'],'57600.00')
        self.assertFalse(row(restored)['unit_price_manual'])
        row(restored).update(price=None,unit_price_manual=True)
        self.assertIsNone(row(evaluate(restored))['price'])
