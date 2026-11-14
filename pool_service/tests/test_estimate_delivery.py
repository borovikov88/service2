"""Synthetic delivery capacities, quoted materials and frozen estimates."""
from copy import deepcopy
from decimal import Decimal as D
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from pool_service.models import Organization, OrganizationAccess
from pool_service.services import estimate_delivery as delivery, pool_estimates as engine
from pool_service.services.estimate_catalog import CatalogUnavailable
from .test_estimate_construction import NEW, row
from .test_pool_estimates import block

CAPACITIES = {'delivery_sand_capacity':'3', 'delivery_concrete_capacity':'12',
              'delivery_rebar_capacity':'1', 'delivery_gravel_capacity':'10',
              'delivery_gravel_volume':'13'}


def calculated():return engine.generate({**NEW,**CAPACITIES})


def old_snapshot():
    s=calculated();s.pop('delivery_version')
    for variant in s['variants'].values():
        variant.pop('delivery_initialized',None)
        for b in variant['blocks']:
            b['rows']=[r for r in b['rows'] if r['id']!='delivery_gravel']
            for r in b['rows']:
                if r.get('basis') in delivery.BASES:r.update(basis='',quantity=None,auto_quantity=False,selection_note='',delivery_computed_note='')
    return engine.evaluate(s)


class DeliveryTests(SimpleTestCase):
    def test_four_trip_counts_follow_rounded_purchases_and_separate_concrete_stages(self):
        s=calculated()
        for tier in s['variants']:
            for material,expected in [('sand','2'),('concrete','3'),('rebar','2'),('gravel','2')]:
                r=row(s,'delivery_'+material,tier)
                self.assertEqual(D(r['quantity']),D(expected))
                self.assertEqual(r['unit'],'рейс');self.assertTrue(r['auto_quantity'])
                self.assertIsNone(r['price']);self.assertIsNone(r['amount'])
            # 16m³ M300 and 6m³ M250 require 2+1 trips of 12m³;
            # pooling both grades would incorrectly produce 2 trips.
            self.assertIn('маркам/этапам',row(s,'delivery_concrete',tier)['selection_note'])
        self.assertEqual(engine.evaluate(json.loads(json.dumps(s)))['variants'],s['variants'])

    def test_actual_manual_material_quantities_and_variant_isolation(self):
        s=calculated()
        row(s,'material_sand').update(auto_quantity=False,quantity='4.8')
        row(s,'material_rebar').update(auto_quantity=False,quantity='2.01')
        row(s,'material_concrete_bowl').update(auto_quantity=False,quantity='1')
        row(s,'material_concrete_base').update(auto_quantity=False,quantity='1')
        s=engine.evaluate(s)
        self.assertEqual([D(row(s,'delivery_'+k)['quantity']) for k in ('sand','rebar','concrete')],[D(1),D(3),D(2)])
        for tier in ('standard','premium'):
            self.assertEqual([D(row(s,'delivery_'+k,tier)['quantity']) for k in ('sand','rebar','concrete')],[D(2),D(2),D(3)])

    def test_kg_tonnes_and_explicit_sand_density(self):
        s=calculated()
        row(s,'material_sand').update(auto_quantity=False,quantity='4800',unit='кг',unit_price_manual=True,price='1')
        row(s,'material_rebar').update(auto_quantity=False,quantity='1000.1',unit='кг',unit_price_manual=True,price='1')
        s=engine.evaluate(s)
        self.assertEqual(D(row(s,'delivery_sand')['quantity']),D(1))
        self.assertEqual(D(row(s,'delivery_rebar')['quantity']),D(2))
        s['inputs']['construction_sand_density']=None
        self.assertIsNone(row(engine.evaluate(s),'delivery_sand')['quantity'])
        row(s,'material_sand').update(quantity='3',unit='м³')
        self.assertEqual(D(row(engine.evaluate(s),'delivery_sand')['quantity']),D(1))

    def test_unknown_inputs_stay_unknown_but_explicit_zero_needs_no_capacity(self):
        s=engine.generate(NEW)
        for material in delivery.MATERIALS:self.assertIsNone(row(s,'delivery_'+material)['quantity'])
        s=engine.recalculate(s,{**s['inputs'],**CAPACITIES,'delivery_gravel_volume':None})
        self.assertIsNone(row(s,'delivery_gravel')['quantity'])
        for rid in ('material_sand','material_concrete_bowl','material_concrete_base','material_rebar'):
            row(s,rid).update(auto_quantity=False,quantity='0')
        s['inputs'].update({k:None for k in CAPACITIES});s['inputs']['delivery_gravel_volume']='0'
        s=engine.evaluate(s)
        for material in delivery.MATERIALS:self.assertEqual(D(row(s,'delivery_'+material)['quantity']),D(0))

    def test_excluded_existing_deleted_materials_and_excluded_block_are_not_delivered(self):
        s=calculated();row(s,'material_sand')['excluded']=True;row(s,'material_rebar')['existing']=True
        materials=block(s,'construction_materials')
        materials['rows']=[r for r in materials['rows'] if r['id'] in {'material_sand','material_rebar','material_insulation','material_supplies'}]
        s=engine.evaluate(s)
        for material in ('sand','rebar','concrete'):self.assertEqual(D(row(s,'delivery_'+material)['quantity']),D(0))
        block(s,'construction_materials')['excluded']=True
        self.assertEqual(D(row(engine.evaluate(s),'delivery_gravel')['quantity']),D(0))

    def test_missing_quantity_or_unsupported_units_never_become_guessed_mass(self):
        for unit,quantity in [('меш.','5'),('т',None)]:
            s=calculated();row(s,'material_sand').update(auto_quantity=False,unit=unit,quantity=quantity,unit_price_manual=True)
            self.assertIsNone(row(engine.evaluate(s),'delivery_sand')['quantity'])
        s=calculated();row(s,'material_concrete_base').update(auto_quantity=False,unit='т',quantity='5',unit_price_manual=True)
        self.assertIsNone(row(engine.evaluate(s),'delivery_concrete')['quantity'])

    def test_manual_delivery_quantity_rate_unit_exclusion_and_deletion_are_preserved(self):
        s=calculated();r=row(s,'delivery_sand')
        r.update(auto_quantity=False,quantity='7',price='777',unit_price_manual=True,unit='компл.',excluded=True,reason='Ручная доставка')
        construction=block(s,'construction');construction['rows']=[r for r in construction['rows'] if r['id']!='delivery_gravel']
        for _ in range(2):s=engine.recalculate(s,{**s['inputs'],'length':'9'})
        r=row(s,'delivery_sand')
        self.assertEqual((r['quantity'],r['price'],r['unit'],r['reason']),('7.000','777.00','компл.','Ручная доставка'))
        self.assertTrue(r['excluded'])
        self.assertFalse(any(r['id']=='delivery_gravel' for r in block(s,'construction')['rows']))
        self.assertTrue(any(r['id']=='delivery_gravel' for r in block(s,'construction','standard')['rows']))

    def test_manual_trip_price_keeps_auto_quantity_and_invalid_auto_unit_does_not_charge(self):
        s=calculated();row(s,'delivery_rebar').update(price='1000',unit_price_manual=True)
        s=engine.evaluate(s);self.assertEqual(row(s,'delivery_rebar')['amount'],'2000.00')
        row(s,'material_rebar').update(auto_quantity=False,quantity='2.01')
        s=engine.evaluate(s)
        self.assertTrue(row(s,'delivery_rebar')['auto_quantity']);self.assertEqual(row(s,'delivery_rebar')['price'],'1000.00')
        self.assertEqual(row(s,'delivery_rebar')['amount'],'3000.00')
        row(s,'delivery_rebar')['unit']='т'
        s=engine.evaluate(s);self.assertIsNone(row(s,'delivery_rebar')['quantity']);self.assertIsNone(row(s,'delivery_rebar')['amount'])

    def test_manual_trip_quantity_replaces_only_its_old_computed_explanation(self):
        for note in (None,'Комментарий диспетчера'):
            s=calculated();r=row(s,'delivery_sand');r.update(auto_quantity=False,quantity='99',price='123')
            if note is not None:r['selection_note']=note
            s=engine.evaluate(s)
            self.assertEqual(D(row(s,'delivery_sand')['quantity']),D(99))
            self.assertEqual(row(s,'delivery_sand')['selection_note'],note or 'Количество рейсов задано вручную.')

    def test_history_passive_then_explicit_upgrade_adopts_only_untouched_placeholders(self):
        s=old_snapshot();row(s,'delivery_sand').update(quantity='7',price='1000')
        row(s,'delivery_rebar').update(price='555',unit_price_manual=True)
        s=engine.evaluate(s);before=deepcopy(s)
        self.assertEqual(engine.evaluate(s),before)
        self.assertIsNone(row(s,'delivery_concrete')['quantity'])
        self.assertNotIn('delivery_version',s)
        changed=engine.recalculate(s,s['inputs'])
        self.assertEqual(D(row(changed,'delivery_concrete')['quantity']),D(3))
        self.assertEqual(row(changed,'delivery_sand')['quantity'],'7.000')
        self.assertIsNone(row(changed,'delivery_rebar')['quantity'])
        self.assertEqual(row(changed,'delivery_rebar')['price'],'555.00')
        self.assertTrue(any(r['id']=='delivery_gravel' for r in block(changed,'construction')['rows']))
        self.assertEqual(s,before)

    def test_capacity_validation_and_no_unquoted_gravel_material_cost(self):
        for field in CAPACITIES:
            for value in ('-1',True,'NaN'):
                with self.assertRaises(engine.EstimateInputError):engine.generate({**NEW,field:value})
        with self.assertRaises(engine.EstimateInputError):engine.generate({**NEW,'delivery_sand_capacity':'0'})
        a=calculated();b=engine.recalculate(a,{**a['inputs'],'delivery_gravel_volume':'999'})
        self.assertEqual(a['variants']['economy']['total'],b['variants']['economy']['total'])
        self.assertFalse(any(r['id']=='material_gravel' for r in block(b,'construction_materials')['rows']))

    def test_extreme_valid_capacity_does_not_create_an_unsaveable_quantity(self):
        s=engine.generate({**NEW,'delivery_gravel_volume':'1000000','delivery_gravel_capacity':'.001'})
        self.assertIsNone(row(s,'delivery_gravel')['quantity'])
        self.assertIn('предел',row(s,'delivery_gravel')['selection_note'])
        self.assertEqual(engine.evaluate(s)['variants'],s['variants'])
        s=engine.recalculate(s,{**s['inputs'],'delivery_gravel_volume':'1000'})
        self.assertEqual(D(row(s,'delivery_gravel')['quantity']),D('1000000'))
        self.assertEqual(engine.evaluate(s)['variants'],s['variants'])


class DeliveryApiTests(TestCase):
    def setUp(self):
        self.org=Organization.objects.create(name='Synthetic delivery tenant')
        user=User.objects.create_user('delivery-test-owner');OrganizationAccess.objects.create(user=user,organization=self.org,role='owner')
        self.client.force_login(user);self.url=reverse('pool_estimate_api')
        mocked=patch('pool_service.estimate_views.get_catalog',side_effect=CatalogUnavailable('Synthetic catalogue unavailable'))
        mocked.start();self.addCleanup(mocked.stop)

    def post(self,**data):
        response=self.client.post(self.url,json.dumps({'organization':self.org.pk,**data}),content_type='application/json')
        self.assertIn(response.status_code,(200,201),response.content[:500]);return response.json()

    def test_save_load_history_and_manual_rate_survive_recalculation(self):
        s=self.post(action='generate',inputs={**NEW,**CAPACITIES})['snapshot']
        row(s,'delivery_sand').update(price='1000',unit_price_manual=True)
        s=self.post(action='validate',snapshot=s)['snapshot']
        self.assertEqual(row(s,'delivery_sand')['amount'],'2000.00')
        saved=self.post(action='save',title='Synthetic delivery trips',snapshot=s,expected_revision=0)
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id']}).json()['snapshot']
        changed=self.post(action='recalculate',snapshot=loaded,inputs={**loaded['inputs'],'delivery_sand_capacity':'10'})['snapshot']
        self.assertEqual(row(changed,'delivery_sand')['amount'],'1000.00')
        self.assertEqual(row(changed,'delivery_sand')['price'],'1000.00')
        self.assertTrue(row(changed,'delivery_sand')['auto_quantity'])
        historical=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(loaded,historical)
