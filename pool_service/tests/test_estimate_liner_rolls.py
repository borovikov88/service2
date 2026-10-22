from copy import deepcopy
from decimal import Decimal as D
from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog, pool_estimates as engine
from pool_service.services.estimate_liner import area
from pool_service.services import estimate_liner_corner as corner
from .test_estimate_catalog import feed, offer
from .test_estimate_liner import film
from .test_pool_estimates import INPUTS, block


def item(pid,name,price='10000',params='',description='',brand='NewBrand',url=None):
    return offer(pid,name,price,description=description,brand=brand,url=url).replace('</offer>',params+'</offer>')


def stock():
    return catalog.parse_catalog(feed(
        item(11,'Плёнка NewBrand 1.65х25.2 м','10000'),
        item(12,'Лайнер OtherBrand 2.00x20 м','12000',brand='OtherBrand'),
        item(13,'Плёнка с неизвестным рулоном','9999'),
        item(14,'Лайнер м² 1.65x25.2 м','300',params='<param name="Единица цены">м²</param>'),
        item(15,'Угол крепежный внешний ПВХ Aquaviva 0050032 2 м','900',brand='Aquaviva',url=corner.URL).replace('</offer>','<picture>https://shop.aqualine22.ru/image/catalog/corner.jpg</picture></offer>'),
    ),checked_at='2026-10-22T00:00:00+00:00')


def choose(s,pid,data=None):
    data=data or stock();catalog.apply_product(film(s),data['products'][str(pid)],data);return engine.evaluate(s)


class GenericLinerTests(SimpleTestCase):
    def test_every_brand_is_searchable_and_generic_roll_converts_exactly(self):
        data=stock();found=catalog.search_catalog(data,'','liner','',engine.normalize_inputs(INPUTS),D('8'))
        self.assertEqual({p['id'] for p in found['products']},{'11','12','13','14'})
        s=choose(engine.generate({**INPUTS,'finish':'film'}),11);r=film(s)
        self.assertEqual(D(r['catalog']['roll_area']),D('41.58'))
        self.assertEqual(D(r['amount']),(D(r['quantity'])*10000/D('41.58')).quantize(D('.01')))
        self.assertIn('Справочно: 2 целых рулонов',r['price_conversion_note'])
        self.assertEqual(r['unit'],'м²')
        self.assertNotEqual(D(r['amount']),D('20000'))
        self.assertEqual(engine.evaluate(s)['variants'],s['variants'])
        changed=engine.recalculate(s,{**s['inputs'],'length':'15'})
        self.assertGreater(D(film(changed)['quantity']),D(r['quantity']))
        self.assertEqual(film(changed)['catalog']['id'],'11')

    def test_structured_and_labelled_dimensions_mm_cm_and_conflicts(self):
        cases=[
            ('Лайнер тест','<param name="Ширина рулона, мм">1650</param><param name="Длина рулона, м">25.2</param>','',D('41.58')),
            ('Пленка тест 165х2520 см','','',D('41.58')),
            ('Пленка тест','','Ширина рулона: 1,65 м. Длина рулона: 25,2 м.',D('41.58')),
            ('Плёнка 1.65х25.2х1.5 мм','','',None),
            ('Плёнка 1.65х25.2 м','<param name="Ширина рулона, м">2</param>','',None),
            ('Пленка тест','<param name="Ширина рулона, м">1.65</param><param name="Ширина рулона, м">2</param><param name="Длина рулона, м">25</param>','',None),
        ]
        for name,params,desc,want in cases:
            data=catalog.parse_catalog(feed(item(22,name,params=params,description=desc)))
            p=data['products']['22'];got=area(p)
            self.assertEqual(D(got) if got else None,want,name+params)

    def test_unknown_and_conflicting_roll_can_be_selected_without_false_m2_price(self):
        s=choose(engine.generate(INPUTS),13);r=film(s)
        self.assertEqual(r['catalog']['id'],'13');self.assertIsNone(r['price']);self.assertIsNone(r['amount'])
        self.assertIn('не подтверждены',r['price_conversion_note'])
        r.update(price='123',unit_price_manual=True)
        self.assertEqual(D(film(engine.evaluate(s))['amount']),D(r['quantity'])*123)
        s=choose(engine.generate(INPUTS),14);r=film(s)
        self.assertEqual(r['price'],'300.00');self.assertEqual(D(r['amount']),D(r['quantity'])*300)
        self.assertIn('справочно 2 целых рулонов',r['price_conversion_note'])

    def test_frozen_dimensions_price_refresh_manual_zero_clear_and_history(self):
        data=stock();s=choose(engine.generate(INPUTS),11,data);original=deepcopy(s)
        changed=deepcopy(data);changed['products']['11']['price']='20000.00'
        changed['products']['11']['liner'].update(width='2',length='20',area='40')
        s=catalog.auto_select(engine.recalculate(s,s['inputs']),changed)[0]
        self.assertEqual(film(s)['catalog']['roll_area'],film(original)['catalog']['roll_area'])
        refreshed,updated,skipped=catalog.refresh_prices(s,changed,'economy')
        self.assertEqual(updated,0);self.assertEqual(skipped,1)
        self.assertEqual(film(refreshed)['price'],film(original)['price'])
        for price in ('0',None,'17'):
            edited=deepcopy(s);film(edited).update(price=price,unit_price_manual=True)
            checked=catalog.refresh_prices(engine.recalculate(edited,edited['inputs']),data,'economy')[0]
            self.assertEqual(film(checked)['price'],None if price is None else price+'.00')
        self.assertEqual(original,choose(engine.generate(INPUTS),11,data))

    def test_corner_real_product_image_price_and_perimeter_without_duplicates(self):
        s=catalog.auto_select(engine.generate({**INPUTS,'finish':'film'}),stock())[0]
        get=lambda snap:next(r for r in block(snap,'finish')['rows'] if r['id']==corner.ID)
        r=get(s);self.assertEqual(r['catalog']['url'],corner.URL)
        self.assertTrue(r['catalog']['image'].endswith('corner.jpg'))
        self.assertEqual((r['quantity'],r['price'],r['amount']),('12','900.00','10800.00'))
        self.assertEqual(r['catalog']['sku'],'')
        s=engine.recalculate(s,{**s['inputs'],'length':'8.1'})
        self.assertEqual(get(s)['quantity'],'13');self.assertEqual(get(s)['amount'],'11700.00')
        for _ in range(2):s=catalog.auto_select(engine.recalculate(s,s['inputs']),stock())[0]
        self.assertEqual(sum(r['id']==corner.ID for r in block(s,'finish')['rows']),1)
        get(s).update(price='0',unit_price_manual=True,excluded=True)
        checked=catalog.auto_select(engine.recalculate(s,s['inputs']),stock())[0]
        self.assertEqual(get(checked)['price'],'0.00');self.assertTrue(get(checked)['excluded'])

    def test_square_metre_or_explicit_other_price_unit_is_not_a_roll_price(self):
        from pool_service.services.pool_estimates import EstimateInputError
        data=stock();s=engine.generate(INPUTS);r=film(s);r.update(unit='рулон',quantity='2',auto_quantity=False)
        before=deepcopy(r)
        with self.assertRaises(EstimateInputError):catalog.apply_product(r,data['products']['14'],data)
        self.assertEqual(r,before)
        data=catalog.parse_catalog(feed(item(22,'Лайнер 2x20 м','12000',params='<param name="Единица цены">пог.м</param>')))
        checked=choose(engine.generate(INPUTS),22,data)
        self.assertIsNone(film(checked)['price']);self.assertIsNone(film(checked)['amount'])
        self.assertIn('единица цены',film(checked)['price_conversion_note'])


from django.test import TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from unittest.mock import patch
from pool_service.models import Organization, OrganizationAccess
import json


class GenericLinerApiTests(TestCase):
    def setUp(self):
        self.org=Organization.objects.create(name='Synthetic liner tenant')
        user=User.objects.create_user('liner-test-owner');OrganizationAccess.objects.create(user=user,organization=self.org,role='owner')
        self.client.force_login(user);self.url=reverse('pool_estimate_api');self.data=stock()
        mock=patch('pool_service.estimate_views.get_catalog',return_value=self.data);mock.start();self.addCleanup(mock.stop)

    def post(self,**data):
        response=self.client.post(self.url,json.dumps({'organization':self.org.pk,**data}),content_type='application/json')
        self.assertIn(response.status_code,(200,201),response.content[:500]);return response.json()

    def test_catalog_every_liner_save_load_recalculate_print_and_unknown_selection(self):
        s=self.post(action='generate',inputs={**INPUTS,'finish':'film'})['snapshot']
        found=self.post(action='catalog_search',inputs=s['inputs'],kind='liner')
        self.assertEqual(found['total'],4)
        s=self.post(action='catalog_apply',snapshot=s,variant='economy',block='finish',row='finish_material',product_id='12')['snapshot']
        amount=film(s)['amount'];saved=self.post(action='save',snapshot=s,expected_revision=0)
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(film(loaded)['amount'],amount)
        for _ in range(2):loaded=self.post(action='recalculate',snapshot=loaded,inputs=loaded['inputs'])['snapshot']
        self.assertEqual(film(loaded)['amount'],amount)
        printed=self.client.get(reverse('pool_estimate_print',args=[saved['id'],1]))
        self.assertContains(printed,'Справочно: 2 целых рулонов')
        self.assertContains(printed,'Стоимость только нужных м²')
        r=next(r for r in block(loaded,'finish')['rows'] if r['id']==corner.ID)
        self.assertEqual(r['catalog']['url'],corner.URL);self.assertEqual(r['price'],'900.00')
        unknown=self.post(action='catalog_apply',snapshot=loaded,variant='economy',block='finish',row='finish_material',product_id='13')['snapshot']
        self.assertIsNone(film(unknown)['amount'])
        old=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(film(old)['amount'],amount)


class LinerPriceUnitRefreshTests(SimpleTestCase):
    def test_m2_price_never_becomes_roll_price_without_reselection(self):
        data=stock();s=choose(engine.generate(INPUTS),14,data);before=deepcopy(film(s))
        changed=deepcopy(data);changed['products']['14']['price']='12000.00';changed['products']['14']['liner']['price_unit']='roll'
        result,updated,skipped=catalog.refresh_prices(s,changed,'economy')
        self.assertEqual((updated,skipped),(0,1))
        self.assertEqual(film(result)['price'],before['price']);self.assertEqual(film(result)['amount'],before['amount'])
        self.assertIn('Единица цены',film(result)['price_conversion_note'])
        adopted=choose(result,14,changed)
        self.assertNotEqual(film(adopted)['price'],before['price'])

    def test_roll_row_refresh_rejects_new_price_per_m2(self):
        data=stock();s=engine.generate(INPUTS);r=film(s);r.update(unit='рулон',quantity='2',auto_quantity=False)
        s=choose(s,11,data);before=deepcopy(film(s))
        changed=deepcopy(data);changed['products']['11']['price']='300.00';changed['products']['11']['liner']['price_unit']='m2'
        result,updated,skipped=catalog.refresh_prices(s,changed,'economy')
        self.assertEqual((updated,skipped),(0,1));self.assertEqual(film(result)['amount'],before['amount'])

    def test_converted_m2_keeps_price_if_new_roll_sale_unit_is_explicitly_unknown(self):
        data=stock();s=choose(engine.generate(INPUTS),11,data);before=deepcopy(film(s))
        changed=deepcopy(data);changed['products']['11']['price']='90000.00';changed['products']['11']['liner'].update(price_unit='unknown',unit_explicit=True)
        result,updated,skipped=catalog.refresh_prices(s,changed,'economy')
        self.assertEqual((updated,skipped),(0,1));self.assertEqual(film(result)['amount'],before['amount'])
        self.assertEqual(film(result)['price'],before['price']);self.assertIn('Площадь рулона',film(result)['price_conversion_note'])
