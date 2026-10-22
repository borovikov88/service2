from copy import deepcopy
from decimal import Decimal as D
from django.test import SimpleTestCase
from pool_service.services import pool_estimates as engine, estimate_catalog as catalog, estimate_plaster_catalog as material
from pool_service.services import estimate_finishing_work as finishing, estimate_geotextile as geotextile
from pool_service.services.estimate_layout import sections
from .test_pool_estimates import INPUTS, block
from .test_estimate_catalog import offer, feed

PARAMS={**INPUTS,'finish':'film','bowl_height':'1.7','tech_plaster':'walls_floor','tech_plaster_thickness':'.02'}


def row(s,code,tier='standard'):
    return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r.get('work_code')==code or r['id']==code)


def fixture():
    return catalog.parse_catalog(feed(
        offer(1,'Геотекстиль Aquaviva 300 г/м2 (50 м)','100',description='Для строительства бассейнов и дорог. Защита лайнера.',url=geotextile.PREFERRED_URL),
        offer(2,'Геотекстиль под пленку бассейна','1',description=''),
        offer(3,'Штукатурка Litokol CR30 (25 кг)','1000',brand='Litokol',description=''),
        offer(4,'Добавка Litokol IDROKOL X20 20 кг','20000',brand='Litokol',description=''),
        offer(5,'Добавка Litokol IDROKOL X20 20 л','1',brand='Litokol',description=''),
    ),checked_at='2026-10-22T00:00:00+00:00')


def selected():return catalog.auto_select(engine.generate(PARAMS),fixture())[0]


class ContactWorkTests(SimpleTestCase):
    def test_same_plaster_area_all_quotes_250_and_exact_delta(self):
        s=engine.generate(PARAMS)
        for tier in s['variants']:
            r=row(s,'contact_layer',tier);p=row(s,'plaster',tier)
            self.assertEqual(r['quantity'],p['quantity']);self.assertEqual(r['quantity'],'73.000')
            self.assertEqual((r['unit'],r['price'],r['amount']),('м²','250.00','18250.00'))
            copy=deepcopy(s);row(copy,'contact_layer',tier)['excluded']=True
            self.assertEqual(D(s['variants'][tier]['total'])-D(engine.evaluate(copy)['variants'][tier]['total']),D('18250'))
        self.assertEqual(engine.evaluate(s)['variants'],s['variants'])

    def test_manual_plaster_quantity_contact_manual_prices_and_toggle(self):
        s=engine.generate(PARAMS);row(s,'plaster').update(quantity='100',auto_quantity=False)
        self.assertEqual(row(engine.evaluate(s),'contact_layer')['quantity'],'100.000')
        for price in ('0',None,'777'):
            copy=deepcopy(s);row(copy,'contact_layer').update(price=price,unit_price_manual=True)
            copy=engine.recalculate(copy,copy['inputs']);self.assertEqual(row(copy,'contact_layer')['price'],None if price is None else price+'.00')
        row(s,'contact_layer').update(quantity='20',auto_quantity=False)
        self.assertEqual(row(engine.evaluate(s),'contact_layer')['quantity'],'20.000')
        off=engine.recalculate(s,{**s['inputs'],'tech_plaster':'off'});self.assertTrue(row(off,'contact_layer')['excluded'])
        on=engine.recalculate(off,{**s['inputs'],'tech_plaster':'walls'});self.assertFalse(row(on,'contact_layer')['excluded'])
        self.assertEqual(row(on,'contact_layer')['quantity'],'20.000')

    def test_order_new_quotes_manual_order_preserved_deleted_contact_not_recreated(self):
        s=engine.generate(PARAMS)
        for v in s['variants'].values():
            work=next(b for sec in sections(v) for b in sec['blocks'] if b['id']=='installation_work')['rows']
            codes=[r.get('work_code') for r in work]
            wanted=['primer','contact_layer','plaster','embedded_work','finish_film','underlay_film','equipment_work','finish_transport','finish_travel']
            self.assertEqual([c for c in codes if c in wanted],wanted)
        v=s['variants']['standard'];v['view_order']['installation_work']=list(reversed(v['view_order']['installation_work']))
        order=deepcopy(v['view_order']);checked=engine.recalculate(s,s['inputs']);self.assertEqual(checked['variants']['standard']['view_order'],order)
        b=block(checked,'finish','standard');b['rows']=[r for r in b['rows'] if r['id']!=finishing.ID]
        checked=engine.recalculate(checked,checked['inputs']);self.assertFalse(any(r['id']==finishing.ID for r in block(checked,'finish','standard')['rows']))

    def test_old_snapshot_not_augmented_on_read_and_recalc_adds_once(self):
        old=engine.generate(PARAMS);old.pop('finishing_work_version')
        for v in old['variants'].values():
            v.pop('contact_work_initialized')
            for b in v['blocks']:b['rows']=[r for r in b['rows'] if r['id']!=finishing.ID]
            v['view_order']={}
        self.assertFalse(any(r.get('work_code')=='contact_layer' for r in block(engine.evaluate(old),'finish')['rows']))
        new=engine.recalculate(old,old['inputs'])
        self.assertEqual(sum(r.get('work_code')=='contact_layer' for r in block(new,'finish')['rows']),1)
        self.assertIn('IDROKOL',row(new,'tech_plaster_contact')['name'])


class LitokolCatalogueTests(SimpleTestCase):
    def test_geotextile_preferred_url_per_m2_and_litokol_confirmed_weight_price(self):
        s=selected()
        for tier in s['variants']:
            g=row(s,'finish_geotextile',tier)
            self.assertEqual(g['catalog']['url'],geotextile.PREFERRED_URL)
            self.assertEqual(g['price'],'100.00');self.assertEqual(D(g['amount']),D(g['quantity'])*100)
        mix=row(s,'tech_plaster_material');latex=row(s,'tech_plaster_latex')
        self.assertEqual((mix['catalog']['id'],mix['quantity'],mix['price'],mix['amount']),('3','98','1000.00','98000.00'))
        self.assertEqual((latex['catalog']['id'],latex['price']),('4','1000.00'))
        self.assertEqual(D(latex['amount']),D(latex['quantity'])*1000)
        self.assertIsNone(row(s,'tech_plaster_contact')['price']);self.assertIsNone(row(s,'tech_plaster_contact')['quantity'])

    def test_litres_never_kg_unknown_price_and_frozen_pack_refresh(self):
        data=fixture();del data['products']['4']
        s=catalog.auto_select(engine.generate(PARAMS),data)[0];r=row(s,'tech_plaster_latex')
        self.assertEqual(r['catalog']['id'],'5');self.assertIsNone(r['price']);self.assertIsNone(r['amount'])
        self.assertIn('Литры',r['selection_note'])
        s=selected();original=deepcopy(s);changed=fixture();changed['products']['4']['plaster_product']['pack_quantity']='10'
        checked,updated,skipped=catalog.refresh_prices(s,changed,'standard')
        self.assertEqual(row(checked,'tech_plaster_latex')['amount'],row(original,'tech_plaster_latex')['amount'])
        self.assertGreaterEqual(skipped,1)
        self.assertIn('Фасовка',row(checked,'tech_plaster_latex')['price_conversion_note'])
        for price in ('0',None,'333'):
            s=deepcopy(original);row(s,'tech_plaster_latex').update(price=price,unit_price_manual=True)
            checked=catalog.auto_select(engine.recalculate(s,s['inputs']),changed)[0]
            self.assertEqual(row(checked,'tech_plaster_latex')['price'],None if price is None else price+'.00')

    def test_exact_dose_amount_without_rounded_kg_price_and_explicit_reselection(self):
        data=fixture();data['products']['4']['price']='20001.00';s=catalog.auto_select(engine.generate(PARAMS),data)[0]
        r=row(s,'tech_plaster_latex');want=(D(r['quantity'])*D('20001')/20).quantize(D('.01'))
        self.assertEqual(D(r['amount']),want)
        new=deepcopy(data);new['products']['4']['price']='30000.00'
        checked,_,_=catalog.refresh_prices(s,new,'standard');self.assertEqual(row(checked,'tech_plaster_latex')['price'],'1500.00')
        self.assertEqual(row(s,'tech_plaster_latex')['price'],r['price'])

    def test_kg_price_is_suspended_when_row_unit_changes_to_litres(self):
        s=selected();r=row(s,'tech_plaster_latex');r.update(unit='л',quantity='10',auto_quantity=False)
        checked=engine.evaluate(s);r=row(checked,'tech_plaster_latex')
        self.assertIsNone(r['price']);self.assertIsNone(r['amount']);self.assertTrue(r['material_price_suspended'])
        self.assertIn('приостановлена',r['price_conversion_note'])
        self.assertIsNone(row(engine.evaluate(checked),'tech_plaster_latex')['amount'])
        r['unit']='кг';restored=engine.evaluate(checked);self.assertEqual(row(restored,'tech_plaster_latex')['price'],'1000.00')
        for price in ('0','500'):
            r=row(checked,'tech_plaster_latex');r.update(unit='л',price=price,unit_price_manual=True)
            self.assertEqual(D(row(engine.evaluate(checked),'tech_plaster_latex')['amount']),D(price)*10)


import json
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from pool_service.models import Organization, OrganizationAccess


class ContactWorkApiTests(TestCase):
    def setUp(self):
        self.org=Organization.objects.create(name='Synthetic Litokol tenant')
        user=User.objects.create_user('litokol-test-owner');OrganizationAccess.objects.create(user=user,organization=self.org,role='owner')
        self.client.force_login(user);self.url=reverse('pool_estimate_api')
        self.data=fixture();mock=patch('pool_service.estimate_views.get_catalog',return_value=self.data);mock.start();self.addCleanup(mock.stop)

    def post(self,**data):
        response=self.client.post(self.url,json.dumps({'organization':self.org.pk,**data}),content_type='application/json')
        self.assertIn(response.status_code,(200,201),response.content[:500]);return response.json()

    def test_work_order_versions_price_one_all_clear_and_shared_future_rate(self):
        s=self.post(action='generate',inputs=PARAMS)['snapshot']
        initial=deepcopy(s);self.assertEqual(row(s,'contact_layer')['price'],'250.00')
        changed=self.post(action='work_price',snapshot=s,variant='standard',block='finish',row=finishing.ID,price='300',all=False)['snapshot']
        self.assertEqual(row(changed,'contact_layer')['price'],'300.00');self.assertEqual(row(changed,'contact_layer','economy')['price'],'250.00')
        newer=self.post(action='generate',inputs=PARAMS)['snapshot']
        self.assertTrue(all(row(newer,'contact_layer',tier)['price']=='300.00' for tier in newer['variants']))
        saved=self.post(action='save',snapshot=changed,expected_revision=0)
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id']}).json()['snapshot']
        self.assertEqual(row(loaded,'tech_plaster_latex')['catalog']['id'],'4')
        self.assertEqual(loaded['variants']['standard']['view_order'],changed['variants']['standard']['view_order'])
        cleared=self.post(action='work_price',snapshot=loaded,variant='standard',block='finish',row=finishing.ID,price=None,all=True)['snapshot']
        for _ in range(2):cleared=self.post(action='recalculate',snapshot=cleared,inputs=cleared['inputs'])['snapshot']
        self.assertTrue(all(row(cleared,'contact_layer',tier)['price'] is None for tier in cleared['variants']))
        old=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(old,loaded)
        printed=self.client.get(reverse('pool_estimate_print',args=[saved['id'],1])).content.decode()
        self.assertLess(printed.index('data-row="work_primer"'),printed.index('data-row="work_contact_layer"'))
        self.assertLess(printed.index('data-row="work_contact_layer"'),printed.index('data-row="work_plaster"'))
        self.assertEqual(initial['variants']['standard']['total'],engine.evaluate(initial)['variants']['standard']['total'])


class LitokolExactMoneyTests(SimpleTestCase):
    def test_recurring_pack_factor_does_not_move_half_cent(self):
        data=fixture();data['products']['4']['plaster_product']['pack_quantity']='3';data['products']['4']['price']='1.00'
        s=catalog.auto_select(engine.generate(PARAMS),data)[0]
        row(s,'tech_plaster_latex').update(quantity='1.005',auto_quantity=False)
        self.assertEqual(row(engine.evaluate(s),'tech_plaster_latex')['amount'],'0.34')
