"""Regressions for explicit catalogue additions and user-approved defaults."""
from copy import deepcopy
from decimal import Decimal
import json
from django.test import SimpleTestCase, TestCase
from .test_pool_estimates import INPUTS, block
from . import test_estimate_catalog as catalog_tests
from pool_service.services import pool_estimates as engine


def rows(s, kind, tier='economy'):
    return [r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['kind']==kind]


def electrical(s, tier='economy'):
    return next(r for r in block(s,'equipment',tier)['rows'] if r['id']=='electrical_equipment')


class ElectricalAndInletTests(SimpleTestCase):
    def test_voltage_defaults_delta_and_frozen_history(self):
        s=engine.generate({**INPUTS,'power':'230'})
        before=deepcopy(s)
        no_allowance=deepcopy(s); no_allowance.pop('electrical_version')
        for v in no_allowance['variants'].values():
            for b in v['blocks']: b['rows']=[r for r in b['rows'] if r['id']!='electrical_equipment']
        baseline=engine.evaluate(no_allowance)
        for tier in s['variants']:
            r=electrical(s,tier)
            self.assertEqual((r['unit'],r['quantity'],r['price'],r['amount']),('компл.','1.000','15000.00','15000.00'))
            self.assertEqual(Decimal(s['variants'][tier]['total'])-Decimal(baseline['variants'][tier]['total']),15000)
        changed=engine.recalculate(s,{**s['inputs'],'power':'400'})
        self.assertEqual(electrical(changed)['amount'],'20000.00')
        self.assertEqual(s,before)
        self.assertFalse(any(r['id']=='electrical_equipment' for r in block(engine.evaluate(baseline),'equipment')['rows']))
        self.assertIsNone(electrical(engine.generate(INPUTS))['price'])

    def test_manual_hidden_deleted_catalog_and_roundtrip(self):
        for price in (None,'0.00','777.00'):
            s=engine.generate({**INPUTS,'power':'230'})
            electrical(s).update(price=price,quantity='2',excluded=True,reason='Пользователь')
            s=engine.recalculate(engine.evaluate(s),{**s['inputs'],'power':'400'})
            r=electrical(s)
            self.assertEqual((r['price'],r['quantity'],r['excluded']),(price,'2.000',True))
            self.assertEqual(engine.evaluate(json.loads(json.dumps(s))),s)
        s=engine.generate(INPUTS)
        block(s,'equipment')['rows']=[r for r in block(s,'equipment')['rows'] if r['id']!='electrical_equipment']
        for _ in range(2):s=engine.recalculate(s,s['inputs'])
        self.assertFalse(any(r['id']=='electrical_equipment' for r in block(s,'equipment')['rows']))

    def test_unit_restore_and_legacy_collision(self):
        s=engine.generate({**INPUTS,'power':'230'})
        electrical(s)['unit']='шт.'
        s=engine.evaluate(s)
        self.assertIsNone(electrical(s)['price'])
        electrical(s)['unit']='компл.'
        s=engine.evaluate(s)
        self.assertEqual(electrical(s)['price'],'15000.00')
        self.assertFalse(electrical(s)['unit_price_manual'])
        s.pop('electrical_version')
        r=electrical(s)
        r.pop('electrical_managed');r.pop('electrical_computed_price');r['price']=None
        s=engine.recalculate(s,{**s['inputs'],'power':'400'})
        self.assertIsNone(electrical(s)['price'])

    def test_unconfirmed_skimmer_units_do_not_multiply_fittings(self):
        s=engine.generate(INPUTS)
        before=rows(s,'inlet')[0]['quantity']
        rows(s,'skimmer')[0].update(quantity='50',unit='кг',auto_quantity=False)
        self.assertEqual(rows(engine.evaluate(s),'inlet')[0]['quantity'],before)

    def test_skimmer_quantity_and_level_exclusion_restore_without_duplicate(self):
        s=engine.generate(INPUTS)
        base=Decimal(rows(s,'inlet')[0]['filtration_quantity'])
        skimmer=rows(s,'skimmer')[0]
        skimmer.update(quantity=str(Decimal(skimmer['quantity'])*2),auto_quantity=False)
        s=engine.evaluate(s)
        self.assertEqual(Decimal(rows(s,'inlet')[0]['filtration_quantity']),base*2)
        total=Decimal(rows(s,'inlet')[0]['quantity'])
        rows(s,'level_regulator')[0].update(excluded=True,reason='Пользователь')
        s=engine.evaluate(s)
        self.assertEqual(Decimal(rows(s,'inlet')[0]['quantity']),total-1)
        rows(s,'level_regulator')[0].update(excluded=False,reason='')
        for _ in range(2):s=engine.evaluate(json.loads(json.dumps(s)))
        self.assertEqual(Decimal(rows(s,'inlet')[0]['quantity']),total)
        self.assertEqual(Decimal(rows(s,'inlet')[0]['level_quantity']),1)
        rows(s,'inlet')[0].update(quantity='17',auto_quantity=False)
        rows(s,'skimmer')[0]['quantity']='1'
        self.assertEqual(rows(engine.evaluate(s),'inlet')[0]['quantity'],'17.000')


class CatalogueAdditionApiTests(TestCase):
    setUp=catalog_tests.CatalogViewsTests.setUp
    post=catalog_tests.CatalogViewsTests.post

    def test_draft_product_choice_then_one_or_all_additions_and_save_load(self):
        original=engine.generate(INPUTS)
        draft=deepcopy(original)
        block(draft,'equipment')['rows'].append(engine.make_row('new_catalog_product','Новая позиция','шт.','1'))
        result=self.post(action='catalog_apply',snapshot=draft,variant='economy',block='equipment',row='new_catalog_product',product_id='5')
        self.assertEqual(result.status_code,200)
        chosen=next(r for r in block(result.json()['snapshot'],'equipment')['rows'] if r['id']=='new_catalog_product')
        self.assertEqual(chosen['catalog']['id'],'5')
        self.assertEqual(chosen['catalog']['selection'],'manual')
        self.assertFalse(any(r['id']=='new_catalog_product' for r in block(original,'equipment')['rows']))
        for all_quotes in (False,True):
            s=deepcopy(original)
            for tier in s['variants']:
                if all_quotes or tier=='economy':block(s,'equipment',tier)['rows'].append(deepcopy(chosen))
            validated=self.post(action='validate',snapshot=s).json()['snapshot']
            for tier in validated['variants']:
                count=sum(r['id']=='new_catalog_product' for r in block(validated,'equipment',tier)['rows'])
                self.assertEqual(count,int(all_quotes or tier=='economy'))
            saved=self.post(action='save',snapshot=validated,expected_revision=0).json()
            loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
            self.assertEqual(next(r for r in block(loaded,'equipment')['rows'] if r['id']=='new_catalog_product')['catalog'],chosen['catalog'])
