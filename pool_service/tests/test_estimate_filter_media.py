from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import patch
from django.test import SimpleTestCase, TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from pool_service.models import Organization, OrganizationAccess
from pool_service.services import pool_estimates as engine, estimate_catalog as catalog, estimate_filter_media as media
from .test_pool_estimates import INPUTS, block
from .test_estimate_catalog import offer, feed


def param_offer(pid,name,price='100',description='',params='',url=None,brand='Aquaviva'):
    return offer(pid,name,price,description=description,url=url,brand=brand).replace('</offer>',params+'</offer>')


def fixture():
    return catalog.parse_catalog(feed(
        param_offer(1,'Фильтр Synthetic 12 м3/ч','1000','Загрузка песка: 75 кг. Загрузка стеклянного песка: 60 кг. Фракция песка: 0.4–1.5 мм.'),
        param_offer(2,'Песок кварцевый 25 кг, 0.5–1.0 мм','100',description='Для фильтров бассейна.',url=media.QUARTZ_URL),
        param_offer(3,'Песок стеклянный 20 кг, 0.5–1.5 мм','200',description='Для фильтров бассейна.',url=media.GLASS_URL),
        param_offer(4,'Кварцевый песок 10 кг, 0.5–1.0 мм','60',description='Для фильтров бассейна.',brand='Waterco'),
    ),checked_at='2026-10-22T00:00:00+00:00')


def row(s,kind='filter_media',tier='economy'):
    return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['kind']==kind)


def selected(data=None):return catalog.auto_select(engine.generate(INPUTS),data or fixture())[0]


class FilterMediaTests(SimpleTestCase):
    def test_price_and_quantities_all_quotes_and_no_extra_stock(self):
        base=engine.generate(INPUTS);s=selected()
        for tier,price,amount,pid in [('economy','100.00','300.00','2'),('standard','100.00','300.00','2'),('premium','200.00','600.00','3')]:
            r=row(s,tier=tier)
            self.assertEqual((r['quantity'],r['price'],r['amount'],r['catalog']['id']),('3.000',price,amount,pid))
            self.assertEqual(r['filter_parent'],row(s,'filter',tier)['id'])
            copy=deepcopy(s);row(copy,tier=tier)['excluded']=True
            self.assertEqual(Decimal(s['variants'][tier]['total'])-Decimal(engine.evaluate(copy)['variants'][tier]['total']),Decimal(amount))
        self.assertEqual(row(base)['quantity'],None)

    def test_filter_replacement_qty_and_rounding_recompute_on_evaluate(self):
        s=selected();data=fixture();p=data['products']['1'];p['filter_loading']['quartz_kg']='76'
        catalog.apply_product(row(s,'filter'),p,data)
        s=engine.evaluate(s);self.assertEqual(row(s)['quantity'],'4.000')
        row(s,'filter')['quantity']='2';s=engine.evaluate(s);self.assertEqual(row(s)['quantity'],'7.000')
        row(s,'filter')['quantity']='0';s=engine.evaluate(s);self.assertTrue(row(s)['excluded'])
        row(s,'filter')['quantity']='1';s=engine.evaluate(s);self.assertFalse(row(s)['excluded'])

    def test_manual_prices_zero_clear_custom_and_explicit_refresh(self):
        for price in ('0',None,'777'):
            s=selected();r=row(s);r.update(price=price,unit_price_manual=True)
            for _ in range(2):s=engine.recalculate(s,s['inputs']);s=catalog.auto_select(s,fixture())[0]
            self.assertEqual(row(s)['price'],None if price is None else price+'.00')
        s=selected();data=fixture();r=row(s)
        catalog.apply_product(r,data['products']['4'],data)
        self.assertEqual(row(engine.evaluate(s))['quantity'],'8.000')
        changed=deepcopy(data);changed['products']['4']['price']='70.00'
        for _ in range(2):s=catalog.auto_select(engine.evaluate(s),changed)[0]
        self.assertEqual(row(s)['price'],'60.00');self.assertFalse(row(s)['unit_price_manual'])
        s,updated,skipped=catalog.refresh_prices(s,changed,'economy')
        self.assertEqual(row(s)['price'],'70.00');self.assertEqual(skipped,0)
        s,_,skipped=catalog.refresh_prices(s,changed,'economy');self.assertEqual(skipped,0)
        row(s).update(quantity='17',auto_quantity=False)
        row(s,'filter')['quantity']='3';self.assertEqual(row(engine.evaluate(s))['quantity'],'17.000')

    def test_hidden_deleted_history_and_order(self):
        s=selected();original=deepcopy(s)
        v=s['variants']['economy'];v['view_order']={'equipment':[row(s)['id'],'filter','pump']}
        row(s)['excluded']=True
        s=catalog.auto_select(engine.recalculate(s,s['inputs']),fixture())[0]
        self.assertTrue(row(s)['excluded']);self.assertEqual(v['view_order']['equipment'],s['variants']['economy']['view_order']['equipment'])
        bid=block(s);bid['rows']=[r for r in bid['rows'] if r['kind']!='filter_media']
        s=engine.recalculate(s,s['inputs']);self.assertFalse(any(r['kind']=='filter_media' for r in block(s)['rows']))
        old=deepcopy(original);old.pop('filter_media_version')
        for variant in old['variants'].values():
            for b in variant['blocks']:b['rows']=[r for r in b['rows'] if r['kind']!='filter_media']
            variant.pop('filter_media_parents')
        self.assertFalse(any(r['kind']=='filter_media' for r in block(engine.evaluate(old))['rows']))
        self.assertTrue(any(r['kind']=='filter_media' for r in block(engine.recalculate(old,old['inputs']))['rows']))
        self.assertEqual(original,selected())

    def test_known_fraction_mismatch_conflicts_and_other_purpose_not_auto(self):
        for title,description,params in [
            ('Песок кварцевый 25 кг, 2–3 мм','Для фильтров бассейна.',''),
            ('Песок кварцевый 25 кг','Не для бассейнов.',''),
            ('Песок кварцевый 25 кг','Для пескоструйных работ.',''),
            ('Песок кварцевый 25 кг','Для фильтров бассейна.','<param name="Фасовка">20 кг</param>'),
        ]:
            data=catalog.parse_catalog(feed(param_offer(1,'Фильтр Synthetic 12 м3/ч',description='Загрузка песка: 75 кг. Фракция песка: 0.4–1.5 мм.'),param_offer(2,title,description=description,params=params)))
            self.assertIsNone(row(selected(data))['catalog'],title+description)
        s=selected();data=fixture();data['products']['2']['filter_media']['conflict']=True
        result=catalog.auto_select(s,data)[0];self.assertEqual(row(result)['catalog']['id'],'4')
        s=selected();row(s)['catalog']['selection']='manual'
        self.assertEqual(row(catalog.auto_select(s,data)[0])['catalog']['id'],'2')

    def test_unknown_mass_and_pack_are_not_guessed_and_glass_no_ratio(self):
        data=catalog.parse_catalog(feed(param_offer(1,'Фильтр Synthetic 12 м3/ч',description='Загрузка песка: 75 кг.'),param_offer(2,'Песок кварцевый 25 кг',description='Для фильтров бассейна.'),param_offer(3,'Песок стеклянный 0.5–1.5 мм',description='Для фильтров бассейна. На 20% меньше песка.')))
        s=selected(data);self.assertEqual(row(s)['quantity'],'3.000')
        self.assertEqual(row(s,tier='premium')['catalog']['id'],'3');self.assertIsNone(row(s,tier='premium')['quantity'])
        self.assertIn('Коэффициент',row(s,tier='premium')['selection_note'])
        self.assertEqual(data['products']['3']['filter_media']['price_unit'],'unknown')
        data['products']['1']['filter_loading']['glass_kg']='60'
        s=selected(data);self.assertIn('фасовка',row(s,tier='premium')['selection_note'])
        self.assertIsNone(row(s,tier='premium')['amount'])

    def test_structured_loading_and_price_per_kg_no_bag_conversion(self):
        data=catalog.parse_catalog(feed(param_offer(1,'Фильтр Synthetic 12 м3/ч',params='<param name="Загрузка песка, кг">76</param>'),param_offer(2,'Песок кварцевый','10',params='<param name="Единица цены">кг</param>')))
        r=row(selected(data));self.assertEqual((r['unit'],r['quantity'],r['amount']),('кг','76.000','760.00'))
        data=catalog.parse_catalog(feed(param_offer(1,'Фильтр Synthetic 12 м3/ч',description='Загрузка песка: 75 кг.',params='<param name="Загрузка песка, кг">76</param>'),param_offer(2,'Песок кварцевый 25 кг')))
        self.assertIsNone(row(selected(data))['catalog'])

    def test_search_contains_all_media_materials_and_brands(self):
        data=fixture();found=catalog.search_catalog(data,'','filter_media','',engine.normalize_inputs(INPUTS),Decimal('8'))
        self.assertEqual({p['id'] for p in found['products']},{'2','3','4'})
        self.assertEqual(catalog.search_catalog(data,'стеклян','filter_media','',engine.normalize_inputs(INPUTS),Decimal('8'))['total'],1)

    def test_no_duplicate_legacy_media_and_multiple_filters_have_own_load(self):
        old=engine.generate(INPUTS);old.pop('filter_media_version')
        for v in old['variants'].values():
            for b in v['blocks']:b['rows']=[r for r in b['rows'] if r['kind']!='filter_media']
            v['filter_media_parents']=[]
        legacy=engine.make_row('old_sand','Кварцевый песок',unit='кг',quantity='75');legacy['price']='10'
        block(old)['rows'].append(legacy)
        upgraded=engine.recalculate(old,old['inputs'])
        self.assertFalse(any(r['kind']=='filter_media' for r in block(upgraded)['rows']))
        self.assertEqual(next(r for r in block(upgraded)['rows'] if r['id']=='old_sand')['price'],'10.00')
        s=selected();other=deepcopy(row(s,'filter'));other['id']='filter2';other['quantity']='2';block(s)['rows'].append(other)
        s=catalog.auto_select(engine.evaluate(s),fixture())[0]
        loads=[r for r in block(s)['rows'] if r['kind']=='filter_media']
        self.assertEqual({r['filter_parent']:r['quantity'] for r in loads},{'filter':'3.000','filter2':'6.000'})


class FilterMediaApiTests(TestCase):
    def setUp(self):
        self.org=Organization.objects.create(name='Synthetic media tenant')
        user=User.objects.create_user('media-test-owner');OrganizationAccess.objects.create(user=user,organization=self.org,role='owner')
        self.client.force_login(user);self.url=reverse('pool_estimate_api')
        self.data=fixture();self.mock=patch('pool_service.estimate_views.get_catalog',return_value=self.data);self.mock.start();self.addCleanup(self.mock.stop)

    def post(self,**data):
        response=self.client.post(self.url,json.dumps({'organization':self.org.pk,**data}),content_type='application/json')
        self.assertIn(response.status_code,(200,201),response.content[:500]);return response.json()

    def test_choose_recalc_save_load_refresh_and_old_snapshot(self):
        s=self.post(action='generate',inputs=INPUTS)['snapshot']
        rid=row(s)['id'];s=self.post(action='catalog_apply',snapshot=s,variant='economy',block='equipment',row=rid,product_id='4')['snapshot']
        s=self.post(action='recalculate',snapshot=s,inputs=s['inputs'])['snapshot'];self.assertEqual(row(s)['quantity'],'8.000')
        saved=self.post(action='save',title='Synthetic media test',snapshot=s,expected_revision=0)
        old=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id']}).json()['snapshot']
        self.assertEqual(row(old)['amount'],'480.00')
        self.data['products']['4']['price']='80.00'
        for _ in range(2):s=self.post(action='validate',snapshot=s)['snapshot']
        self.assertEqual(row(s)['price'],'60.00')
        for _ in range(2):s=self.post(action='catalog_prices',snapshot=s,variant='economy')['snapshot']
        self.assertEqual(row(s)['price'],'80.00');self.assertFalse(row(s)['unit_price_manual'])
        historical=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(old,historical)

class FilterMediaReviewRegressions(SimpleTestCase):
    def test_invalid_filter_unit_and_fractional_count_do_not_multiply_mass(self):
        for unit,quantity in [('кг','50'),('шт.','1.5')]:
            s=selected();row(s,'filter').update(unit=unit,quantity=quantity)
            result=engine.evaluate(s);self.assertIsNone(row(result)['quantity'])
            self.assertIn('целое количество',row(result)['selection_note'])

    def test_negated_other_purpose_does_not_reject_pool_sand(self):
        data=catalog.parse_catalog(feed(param_offer(1,'Фильтр Synthetic 12 м3/ч',description='Загрузка песка: 75 кг.'),param_offer(2,'Песок кварцевый 25 кг',description='Не для пескоструйной обработки. Для фильтров бассейна.')))
        self.assertEqual(row(selected(data))['catalog']['id'],'2')

    def test_new_conflict_warns_on_manual_selection_and_keeps_price_qty(self):
        data=fixture();s=selected(data);catalog.apply_product(row(s),data['products']['2'],data,'manual')
        row(s).update(quantity='17',auto_quantity=False,price='777',unit_price_manual=True)
        changed=deepcopy(data);changed['products']['2']['filter_media']['conflict']=True
        result=catalog.auto_select(s,changed)[0]
        r=row(result);self.assertEqual((r['catalog']['id'],r['price'],r['quantity']),('2','777.00','17.000'))
        self.assertIn('Противоречивые',r['selection_note'])
        self.assertEqual(row(s)['price'],'777')
        # Explicit refresh of facts can update automatic quantities even though
        # the selected material/price were manually chosen.
        s=selected(data);catalog.apply_product(row(s),data['products']['2'],data,'manual')
        changed=deepcopy(data);changed['products']['1']['filter_loading']['quartz_kg']='101'
        result=catalog.auto_select(s,changed)[0]
        self.assertEqual(row(result)['quantity'],'5.000');self.assertEqual(row(result)['price'],'100.00')

    def test_known_filter_ban_blocks_glass_but_not_quartz(self):
        data=fixture()
        data=catalog.parse_catalog(feed(param_offer(1,'Фильтр Synthetic 12 м3/ч',description='Загрузка песка: 75 кг. Не использовать стеклянный песок.'),param_offer(2,'Песок кварцевый 25 кг'),param_offer(3,'Песок стеклянный 20 кг')))
        s=selected(data);self.assertEqual(row(s)['catalog']['id'],'2');self.assertIsNone(row(s,tier='premium')['catalog'])

    def test_sale_pack_and_frozen_price_are_one_lifecycle(self):
        for selection in ('manual','auto'):
            data=fixture();s=selected(data);row(s)['catalog']['selection']=selection
            changed=deepcopy(data);changed['products']['2']['filter_media']['bag_kg']='10';changed['products']['2']['price']='60.00'
            for _ in range(2):s=catalog.auto_select(engine.evaluate(s),changed)[0]
            r=row(s);self.assertEqual((r['quantity'],r['price'],r['amount']),('3.000','100.00','300.00'))
            self.assertIn('Фасовка',r['selection_note'])
            s,_,_=catalog.refresh_prices(s,changed,'economy')
            r=row(s);self.assertEqual((r['quantity'],r['price'],r['amount']),('8.000','60.00','480.00'))
        s=selected();row(s).update(quantity='17',auto_quantity=False)
        result,_,skipped=catalog.refresh_prices(s,changed,'economy');self.assertEqual(skipped,1)
        self.assertEqual((row(result)['quantity'],row(result)['price']),('17.000','100.00'))

    def test_sale_unit_is_frozen_with_price_until_explicit_refresh(self):
        s=selected();data=fixture();data['products']['2']['filter_media']['price_unit']='kg';data['products']['2']['price']='4.00'
        for _ in range(2):s=catalog.auto_select(s,data)[0]
        r=row(s);self.assertEqual((r['unit'],r['quantity'],r['price'],r['amount']),('меш.','3.000','100.00','300.00'))
        s,_,_=catalog.refresh_prices(s,data,'economy');r=row(s)
        self.assertEqual((r['unit'],r['quantity'],r['price'],r['amount']),('кг','75.000','4.00','300.00'))
