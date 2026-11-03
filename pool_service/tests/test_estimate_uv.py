"""Synthetic optional UV equipment lifecycle and manual catalogue selection."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from pool_service.models import Organization, OrganizationAccess
from pool_service.services import estimate_catalog as catalog, estimate_uv as uv, pool_estimates as engine
from .test_estimate_catalog import feed, offer
from .test_pool_estimates import INPUTS, block


def fixture():
    return catalog.parse_catalog(feed(
        offer(1,'Ультрафиолетовая установка Synthetic U-55','100',description='Установка для бассейна.'),
        offer(2,'Ультрафиолетовая фотокаталитическая установка Synthetic Q-65','200',description='Для бассейна.'),
        offer(3,'Лампа для ультрафиолетовой установки Synthetic U-55','10'),
        offer(4,'Кварцевая колба для УФ-установки Synthetic U-55','20'),
        offer(5,'УФ-установка Synthetic UV-100','300',description='Для бассейна.').replace('own_stock','preorder'),
    ),checked_at='2026-10-25T00:00:00+00:00')


def units(s,tier='economy'):
    return [r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['kind']=='uv']


def enabled():return engine.generate({**INPUTS,'uv_enabled':True})


class OptionalUvTests(SimpleTestCase):
    def test_default_off_and_legacy_open_do_not_add_rows(self):
        s=engine.generate(INPUTS)
        self.assertIs(s['inputs']['uv_enabled'],False)
        old=deepcopy(s);old.pop('uv_version');old['inputs'].pop('uv_enabled')
        for v in old['variants'].values():v.pop('uv_initialized',None)
        before=deepcopy(old)
        result=engine.evaluate(old)
        for tier in result['variants']:
            self.assertEqual(units(result,tier),[])
            self.assertEqual(result['variants'][tier]['total'],s['variants'][tier]['total'])
        self.assertEqual(old,before)

    def test_enabling_adds_one_unpriced_manual_row_in_each_quote(self):
        base=engine.generate(INPUTS)
        s=engine.recalculate(base,{**base['inputs'],'uv_enabled':True})
        for tier in s['variants']:
            self.assertEqual(len(units(s,tier)),1)
            row=units(s,tier)[0]
            self.assertEqual((row['id'],row['name'],row['quantity'],row['unit']),('uv',uv.TITLE,'1.000','шт.'))
            self.assertEqual((row['basis'],row['auto_quantity'],row['model']),('',False,''))
            self.assertIsNone(row.get('catalog'));self.assertIsNone(row['price']);self.assertIsNone(row['amount'])
            self.assertEqual(s['variants'][tier]['total'],base['variants'][tier]['total'])

    def test_generic_auto_select_never_selects_uv_even_when_available(self):
        s=enabled();before=deepcopy(s)
        for _ in range(2):s=catalog.auto_select(engine.recalculate(s,s['inputs']),fixture())[0]
        for tier in s['variants']:
            self.assertEqual(len(units(s,tier)),1)
            self.assertIsNone(units(s,tier)[0].get('catalog'))
            self.assertIsNone(units(s,tier)[0]['price'])
        self.assertIsNone(units(before)[0].get('catalog'))

    def test_manual_product_price_and_quantity_survive_off_on_and_auto_select(self):
        data=fixture();s=enabled();catalog.apply_product(units(s)[0],data['products']['1'],data)
        s=engine.evaluate(s)
        self.assertEqual((units(s)[0]['price'],units(s)[0]['amount']),('100.00','100.00'))
        units(s)[0].update(price='777',unit_price_manual=True,quantity='3',auto_quantity=False,unit='компл.')
        s=engine.recalculate(s,{**s['inputs'],'uv_enabled':False})
        row=units(s)[0]
        self.assertTrue(row['excluded']);self.assertEqual(row['reason'],uv.OFF)
        self.assertIsNone(row['amount']);self.assertEqual((row['price'],row['quantity'],row['unit']),('777.00','3.000','компл.'))
        s=catalog.auto_select(s,data)[0]
        self.assertTrue(units(s)[0]['excluded'])
        s=engine.recalculate(s,{**s['inputs'],'uv_enabled':True})
        row=units(s)[0]
        self.assertFalse(row['excluded']);self.assertEqual((row['catalog']['id'],row['price'],row['quantity'],row['amount']),('1','777.00','3.000','2331.00'))

    def test_user_exclusion_and_deletion_survive_repeated_toggle(self):
        for deletion in (False,True):
            s=enabled()
            if deletion:block(s)['rows']=[r for r in block(s)['rows'] if r['kind']!='uv']
            else:units(s)[0].update(excluded=True,reason='Решение пользователя')
            for state in (True,False,True):
                s=engine.recalculate(s,{**s['inputs'],'uv_enabled':state})
                s=catalog.auto_select(s,fixture())[0]
            if deletion:self.assertEqual(units(s),[])
            else:
                self.assertTrue(units(s)[0]['excluded'])
                self.assertEqual(units(s)[0]['reason'],'Решение пользователя')
            self.assertEqual(len(units(s,'standard')),1)

    def test_manual_search_excludes_parts_and_keeps_order_only_units(self):
        data=fixture()
        result=catalog.search_catalog(data,'','uv','',engine.normalize_inputs(INPUTS),Decimal('8'))
        self.assertEqual({p['id'] for p in result['products']},{'1','2','5'})
        self.assertFalse(data['products']['5']['available'])
        for pid in ('3','4'):self.assertNotEqual(data['products'][pid]['kind'],'uv')
        s=enabled();catalog.apply_product(units(s)[0],data['products']['5'],data)
        self.assertEqual(units(engine.evaluate(s))[0]['catalog']['id'],'5')

    def test_unit_category_can_identify_generic_unit_but_not_lamps_or_spare_parts(self):
        xml=feed(
            offer(1,'Установка Synthetic UV-10').replace('</offer>','<categoryId>3</categoryId></offer>'),
            offer(2,'Лампа Synthetic UV-10').replace('</offer>','<categoryId>3</categoryId></offer>'),
            offer(3,'Ультрафиолетовая установка Synthetic UV-10').replace('</offer>','<categoryId>4</categoryId></offer>'),
        ).replace(b'<offers>',('<categories><category id="1">Бассейны</category><category id="2" parentId="1">Оборудование для бассейнов</category><category id="3" parentId="2">Ультрафиолетовые установки</category><category id="4" parentId="2">Запчасти УФ-установок</category></categories><offers>').encode())
        data=catalog.parse_catalog(xml)
        self.assertEqual(data['products']['1']['kind'],'uv')
        for pid in ('2','3'):self.assertNotEqual(data['products'][pid]['kind'],'uv')

    def test_explicit_price_refresh_and_json_roundtrip_keep_manual_selection(self):
        data=fixture();s=enabled();catalog.apply_product(units(s)[0],data['products']['1'],data)
        s=engine.evaluate(s);data['products']['1']['price']='125.00'
        result=catalog.auto_select(s,data)[0]
        self.assertEqual(units(result)[0]['price'],'100.00')
        result,updated,_=catalog.refresh_prices(result,data,'economy')
        self.assertGreaterEqual(updated,1);self.assertEqual(units(result)[0]['price'],'125.00')
        loaded=engine.evaluate(json.loads(json.dumps(result)))
        self.assertEqual(units(loaded)[0],units(result)[0])

    def test_non_unit_catalog_choice_is_rejected_without_losing_checkbox_control(self):
        data=fixture();s=enabled();row=units(s)[0];before=deepcopy(row)
        with self.assertRaises(engine.EstimateInputError):catalog.apply_product(row,data['products']['3'],data)
        self.assertEqual(row,before)
        catalog.apply_product(row,data['products']['1'],data)
        s=engine.evaluate(s)
        changed=deepcopy(data);changed['products']['1'].update(kind='other',price='999.00')
        result,_,skipped=catalog.refresh_prices(s,changed,'economy')
        self.assertGreaterEqual(skipped,1)
        self.assertEqual((units(result)[0]['kind'],units(result)[0]['price']),('uv','100.00'))
        self.assertIn('полной УФ-установки',units(result)[0]['catalog_review_note'])
        off=engine.recalculate(result,{**result['inputs'],'uv_enabled':False})
        self.assertTrue(units(off)[0]['excluded']);self.assertIsNone(units(off)[0]['amount'])

    def test_legacy_manual_uv_choice_while_off_is_not_charged(self):
        data=fixture();s=engine.generate(INPUTS);s.pop('uv_version');s['inputs'].pop('uv_enabled')
        target=next(r for r in block(s)['rows'] if r['id']=='piping')
        catalog.apply_product(target,data['products']['1'],data)
        result=engine.evaluate(s)
        self.assertTrue(units(result)[0]['excluded'])
        self.assertIsNone(units(result)[0]['amount'])
        self.assertEqual(units(result)[0]['price'],'100.00')

    def test_id_conflict_and_boolean_validation(self):
        s=engine.generate(INPUTS);block(s)['rows'].append(engine.make_row('uv','Ручная позиция'))
        result=engine.recalculate(s,{**s['inputs'],'uv_enabled':True})
        self.assertEqual(units(result)[0]['id'],'uv_1')
        for value in ('yes','false',1,None):
            with self.assertRaises(engine.EstimateInputError):engine.generate({**INPUTS,'uv_enabled':value})


class OptionalUvApiTests(TestCase):
    def setUp(self):
        self.org=Organization.objects.create(name='Synthetic UV tenant')
        user=User.objects.create_user('uv-test-owner');OrganizationAccess.objects.create(user=user,organization=self.org,role='owner')
        self.client.force_login(user);self.url=reverse('pool_estimate_api');self.data=fixture()
        mocked=patch('pool_service.estimate_views.get_catalog',return_value=self.data)
        mocked.start();self.addCleanup(mocked.stop)

    def post(self,**data):
        response=self.client.post(self.url,json.dumps({'organization':self.org.pk,**data}),content_type='application/json')
        self.assertIn(response.status_code,(200,201),response.content[:500]);return response.json()

    def test_manual_choose_price_refresh_save_load_history_and_toggle(self):
        s=self.post(action='generate',inputs={**INPUTS,'uv_enabled':True})['snapshot']
        s=self.post(action='catalog_apply',snapshot=s,variant='economy',block='equipment',row='uv',product_id='1')['snapshot']
        self.assertEqual(units(s)[0]['amount'],'100.00')
        for tier in ('standard','premium'):self.assertIsNone(units(s,tier)[0].get('catalog'))
        saved=self.post(action='save',title='Synthetic manual UV',snapshot=s,expected_revision=0)
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id']}).json()['snapshot']
        self.assertEqual(units(loaded)[0]['catalog']['id'],'1')
        self.data['products']['1']['price']='125.00'
        current=self.post(action='catalog_prices',snapshot=loaded,variant='economy')['snapshot']
        self.assertEqual(units(current)[0]['price'],'125.00')
        current=self.post(action='recalculate',snapshot=current,inputs={**current['inputs'],'uv_enabled':False})['snapshot']
        self.assertTrue(units(current)[0]['excluded']);self.assertIsNone(units(current)[0]['amount'])
        current=self.post(action='recalculate',snapshot=current,inputs={**current['inputs'],'uv_enabled':True})['snapshot']
        self.assertEqual(units(current)[0]['amount'],'125.00')
        historical=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(loaded,historical)
