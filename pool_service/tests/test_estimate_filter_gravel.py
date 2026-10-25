"""Synthetic filter layers, sale packages and saved-estimate regressions."""
from copy import deepcopy
from decimal import Decimal
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from pool_service.models import Organization, OrganizationAccess
from pool_service.services import estimate_catalog as catalog, estimate_filter_media as media
from pool_service.services import estimate_filter_gravel as gravel, pool_estimates as engine
from .test_estimate_filter_media import param_offer, row
from .test_estimate_catalog import feed
from .test_pool_estimates import INPUTS, block


def fixture(gravel_mass='21', sand_mass='26'):
    params='<param name="Загрузка песка, кг">'+sand_mass+'</param>'
    if gravel_mass is not None:params+='<param name="Загрузка гравия, кг">'+gravel_mass+'</param>'
    return catalog.parse_catalog(feed(
        param_offer(1,'Фильтр Synthetic 12 м3/ч','1000',params=params,
                    description='Песок 0.4–0.8 мм; гравий 1–2 мм.'),
        param_offer(2,'Песок кварцевый 25 кг, 0.5–0.8 мм','100',description='Для фильтров бассейна.'),
        param_offer(3,'Песок стеклянный 2–4 мм','160',description='Для фильтров бассейна.',
                    params='<vendorCode>'+media.OWNER_GRAVEL_SKU+'</vendorCode>'),
        param_offer(4,'Песок стеклянный 0.5–0.8 мм','140',description='Для фильтров бассейна.'),
        param_offer(5,'Гравий 1–2 мм, 10 кг','110',description='Для фильтров бассейна.'),
    ),checked_at='2026-10-22T00:00:00+00:00')


def selected(data=None):return catalog.auto_select(engine.generate(INPUTS),data or fixture())[0]


def rows(s,kind='filter_gravel',tier='economy'):
    return [r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['kind']==kind]


def owner_only(data=None):
    data=data or fixture()
    data['products'].pop('5')
    return data


def legacy(s):
    s=deepcopy(s);s.pop('filter_gravel_version',None)
    for v in s['variants'].values():
        v.pop('filter_gravel_active',None);v.pop('filter_gravel_parents',None)
        v.pop('filter_gravel_deleted',None)
        for b in v['blocks']:b['rows']=[r for r in b['rows'] if r['kind']!='filter_gravel']
    return s


class FilterGravelTests(SimpleTestCase):
    def test_separate_mass_fraction_and_owner_role_keep_physical_glass(self):
        data=owner_only();load=data['products']['1']['filter_loading']
        self.assertEqual((load['quartz_kg'],load['gravel_kg']),('26','21'))
        self.assertEqual((load['fraction_min'],load['fraction_max'],load['gravel_fraction_min'],load['gravel_fraction_max']),('0.4','0.8','1','2'))
        self.assertFalse(load['conflict'])
        s=selected(data);r=row(s,'filter_gravel')
        self.assertEqual((r['catalog']['id'],r['catalog']['filter_media']['material'],r['catalog']['filter_media']['bag_kg']),('3','glass','20'))
        self.assertEqual(data['products']['3']['kind'],'filter_media')
        self.assertEqual((r['quantity'],r['amount']),('2.000','320.00'))
        self.assertIn('1–2',r['selection_note']);self.assertIn('2–4',r['selection_note']);self.assertIn('подтверждения',r['selection_note'])
        self.assertEqual(row(s)['catalog']['id'],'2')

    def test_rounds_total_layer_mass_for_two_filters_not_each_filter(self):
        data=owner_only();s=selected(data);row(s,'filter')['quantity']='2'
        s=engine.evaluate(s)
        self.assertEqual((row(s)['quantity'],row(s,'filter_gravel')['quantity']),('3.000','3.000'))
        self.assertIn('42',row(s,'filter_gravel')['selection_note'])

    def test_zero_absent_negative_conflicting_and_total_litres_are_distinct(self):
        for mass in ('0',None):
            data=fixture(mass);s=selected(data)
            self.assertEqual(rows(s),[])
            self.assertEqual(data['products']['1']['filter_loading']['gravel_kg'],mass)
            self.assertEqual(any('отсутствующее поле не означает ноль' in x for x in s['variants']['economy']['warnings']),mass is None)
        for params,description in [
            ('<param name="Загрузка гравия, кг">-5</param>',''),
            ('<param name="Загрузка гравия, кг">10</param><param name="Загрузка гравия, кг">20</param>',''),
            ('','Загрузка гравия: -5 кг.'),
        ]:
            data=catalog.parse_catalog(feed(param_offer(1,'Фильтр Synthetic 12 м3/ч',params=params,description=description)))
            self.assertTrue(data['products']['1']['filter_loading']['conflict'])
            self.assertEqual(rows(selected(data)),[])
        data=catalog.parse_catalog(feed(param_offer(1,'Фильтр Synthetic 12 м3/ч',params='<param name="Объем загрузки, л">100</param><param name="Общая масса загрузки, кг">150</param>')))
        load=data['products']['1']['filter_loading']
        self.assertIsNone(load['quartz_kg']);self.assertIsNone(load['gravel_kg'])

    def test_gravel_own_fraction_and_material_are_not_sand_fraction(self):
        data=fixture();s=selected(data)
        self.assertEqual(row(s,'filter_gravel')['catalog']['id'],'5')
        data['products'].pop('3');data['products'].pop('5')
        s=selected(data);r=row(s,'filter_gravel')
        self.assertIsNone(r['catalog']);self.assertIsNone(r['quantity']);self.assertIsNone(r['amount'])
        self.assertIn('21 кг',r['selection_note'])
        other=deepcopy(data['products']['2']);other['filter_media'].update(fraction_min='1',fraction_max='2')
        self.assertIn('не подтверждён как гравий',gravel.assess(other,row(s,'filter')))

    def test_noun_fraction_description_does_not_mix_support_and_sand(self):
        data=catalog.parse_catalog(feed(param_offer(1,'Фильтр Synthetic 12 м3/ч',
            description='Загрузка песка: 30 кг; загрузка гравия: 10 кг. Песок фракции 0.4–0.8 мм; гравий фракция 1–2 мм.')))
        load=data['products']['1']['filter_loading']
        self.assertFalse(load['conflict'])
        self.assertEqual((load['fraction_min'],load['fraction_max'],load['gravel_fraction_min'],load['gravel_fraction_max']),('0.4','0.8','1','2'))

    def test_card_filter_kind_wins_over_gravel_keyword_in_title(self):
        from .test_estimate_filter_facts import filtr, PARAMS
        data=catalog.parse_catalog(feed(filtr(name='Фильтр Synthetic для песка и гравия 12 м3/ч',params=[*PARAMS,('Загрузка гравия, кг','21')])))
        product=data['products']['1']
        self.assertEqual(product['kind'],'filter')
        self.assertEqual((product['filter_loading']['quartz_kg'],product['filter_loading']['gravel_kg']),('45','21'))

    def test_availability_and_known_glass_ban_block_owner_exception(self):
        for change in ('stock','ban','package','fraction','unknown_fraction'):
            data=owner_only()
            if change=='stock':data['products']['3']['available']=False
            elif change=='ban':data['products']['1']['filter_loading']['denied']=['glass']
            elif change=='package':data['products']['3']['filter_media']['bag_kg']='25'
            elif change=='fraction':data['products']['3']['filter_media'].update(fraction_min='0.5',fraction_max='1')
            else:data['products']['3']['filter_media'].update(fraction_min=None,fraction_max=None)
            s=selected(data);self.assertIsNone(row(s,'filter_gravel')['catalog'])

    def test_owner_sku_does_not_fill_main_sand_when_fraction_unknown(self):
        data=owner_only();data['products']['1']['filter_loading'].update(fraction_min=None,fraction_max=None,glass_kg='20')
        data['products'].pop('4');data['products'].pop('2')
        s=selected(data)
        self.assertIsNone(row(s,tier='premium')['catalog'])
        self.assertEqual(row(s,'filter_gravel',tier='premium')['catalog']['id'],'3')

    def test_legacy_auto_owner_sku_is_cleared_from_sand_but_manual_is_preserved(self):
        data=owner_only();data['products'].pop('2');data['products'].pop('4')
        data['products']['1']['filter_loading'].update(fraction_min=None,fraction_max=None,glass_kg='60')
        for selection in ('auto','manual'):
            s=selected(data)
            catalog.apply_product(row(s,tier='premium'),data['products']['3'],data,selection)
            result=catalog.auto_select(s,data,'premium')[0]
            if selection=='auto':self.assertIsNone(row(result,tier='premium')['catalog'])
            else:
                self.assertEqual(row(result,tier='premium')['catalog']['id'],'3')
                self.assertIn('отдельного гравийного слоя',row(result,tier='premium')['selection_note'])

    def test_deleted_excluded_manual_and_existing_filters_do_not_gain_purchase(self):
        data=owner_only();s=selected(data)
        block(s)['rows']=[r for r in block(s)['rows'] if r['kind']!='filter_gravel']
        s=catalog.auto_select(engine.recalculate(s,s['inputs']),data)[0]
        self.assertEqual(rows(s),[])
        # Editing technical mass on the same selected model does not cancel
        # the user's deletion and silently add another purchase.
        data['products']['1']['filter_loading']['gravel_kg']='31'
        s=catalog.auto_select(s,data,'economy')[0]
        self.assertEqual(rows(s),[])
        for mass in ('0','31'):
            data['products']['1']['filter_loading']['gravel_kg']=mass
            s=catalog.auto_select(s,data,'economy')[0]
            self.assertEqual(rows(s),[])
        s=selected(data);row(s,'filter_gravel')['excluded']=True
        self.assertTrue(row(catalog.auto_select(s,data)[0],'filter_gravel')['excluded'])
        s=selected(data);r=row(s,'filter_gravel');r.update(price='777',unit_price_manual=True,quantity='17',auto_quantity=False)
        row(s,'filter')['existing']=True
        s=catalog.auto_select(engine.recalculate(s,s['inputs']),data)[0]
        r=row(s,'filter_gravel');self.assertEqual((r['price'],r['quantity']),('777.00','17.000'))
        self.assertIn('существует',r['selection_note'])
        s=engine.generate(INPUTS);row(s,'filter')['existing']=True
        self.assertEqual(rows(catalog.auto_select(s,data)[0]),[])

    def test_switching_filter_without_gravel_removes_only_untouched_auto_row(self):
        data=owner_only();s=selected(data)
        data['products']['1']['filter_loading']['gravel_kg']='0'
        result=catalog.auto_select(s,data)[0];self.assertEqual(rows(result),[])
        data['products']['1']['filter_loading']['gravel_kg']='21'
        result=catalog.auto_select(result,data)[0]
        self.assertEqual(row(result,'filter_gravel')['quantity'],'2.000')
        data['products']['1']['filter_loading']['gravel_kg']='0'
        s=selected(owner_only());row(s,'filter_gravel')['catalog']['selection']='manual'
        result=catalog.auto_select(s,data)[0]
        self.assertEqual(row(result,'filter_gravel')['catalog']['id'],'3')
        self.assertIn('не требует гравия',row(result,'filter_gravel')['selection_note'])

    def test_package_unit_and_price_remain_frozen_until_explicit_refresh(self):
        data=fixture();s=selected(data);old=row(s,'filter_gravel')['catalog']['checked_at']
        data['products']['5']['filter_media']['bag_kg']='5';data['products']['5']['price']='60.00'
        for _ in range(2):s=catalog.auto_select(s,data)[0]
        r=row(s,'filter_gravel');self.assertEqual((r['quantity'],r['price'],r['amount']),('3.000','110.00','330.00'))
        self.assertEqual(r['catalog']['checked_at'],old)
        self.assertIn('Фасовка',r['selection_note'])
        s,_,_=catalog.refresh_prices(s,data,'economy')
        r=row(s,'filter_gravel');self.assertEqual((r['quantity'],r['price'],r['amount']),('5.000','60.00','300.00'))
        r.update(quantity='17',auto_quantity=False)
        data['products']['5']['filter_media']['bag_kg']='2'
        s,_,skipped=catalog.refresh_prices(s,data,'economy')
        self.assertGreaterEqual(skipped,1)
        self.assertEqual((row(s,'filter_gravel')['quantity'],row(s,'filter_gravel')['price']),('17.000','60.00'))

    def test_new_manual_glass_mass_fallback_is_gated_and_survives_normalization(self):
        data=fixture(None,'75');s=selected(data)
        catalog.apply_product(row(s),data['products']['4'],data,'manual')
        updated=engine.evaluate(s)
        self.assertEqual((row(updated)['quantity'],row(updated)['amount']),('4.000','560.00'))
        self.assertIn('Предварительно',row(updated)['selection_note']);self.assertIn('Коэффициент',row(updated)['selection_note'])
        self.assertTrue(row(engine.evaluate(updated))['media_sand_mass_fallback'])
        # A historical glass snapshot without the new explicit choice marker
        # must retain its unknown quantity and amount, even when sand mass exists.
        row(s).pop('media_sand_mass_fallback',None)
        for _ in range(2):s=engine.evaluate(s)
        self.assertIsNone(row(s)['quantity']);self.assertIsNone(row(s)['amount'])

    def test_zero_glass_alternative_keeps_required_main_quartz_load(self):
        data=fixture(None,'75');data['products']['1']['filter_loading']['glass_kg']='0'
        s=selected(data)
        self.assertEqual((row(s,tier='premium')['catalog']['id'],row(s,tier='premium')['quantity']),('2','3.000'))

    def test_zero_quartz_alternative_uses_required_glass_or_stays_unpriced(self):
        data=fixture(None,'0');data['products']['1']['filter_loading']['glass_kg']='60'
        s=selected(data)
        for tier in s['variants']:
            self.assertEqual((row(s,tier=tier)['catalog']['id'],row(s,tier=tier)['quantity']),('4','3.000'))
        data['products']['1']['filter_loading']['glass_kg']='0'
        s=selected(data)
        for tier in s['variants']:self.assertIsNone(row(s,tier=tier)['catalog'])

    def test_manual_owner_selection_keeps_gravel_role_and_catalog_search(self):
        data=fixture();s=selected(data);r=row(s,'filter_gravel')
        catalog.apply_product(r,data['products']['3'],data,'manual')
        s=engine.evaluate(s);r=row(s,'filter_gravel')
        self.assertEqual((r['kind'],r['basis'],r['filter_parent'],r['quantity']),('filter_gravel','filter_load','filter','2.000'))
        found=catalog.search_catalog(data,'','filter_gravel','',engine.normalize_inputs(INPUTS),Decimal('8'))
        self.assertEqual({p['id'] for p in found['products']},{'3','5'})

    def test_legacy_history_is_inert_and_selected_variant_activation_is_scoped(self):
        data=owner_only();old=legacy(selected(data));before=engine.evaluate(old)
        for tier in before['variants']:self.assertEqual(rows(before,tier=tier),[])
        gravel.initialize(before,'economy');after=engine.evaluate(before)
        self.assertEqual(len(rows(after)),1)
        for tier in ('standard','premium'):self.assertEqual(rows(after,tier=tier),[])
        again=engine.evaluate(after);self.assertEqual(after,again)

    def test_point_filter_repair_upgrades_only_selected_legacy_quote(self):
        from .test_estimate_filter_facts import filtr, PARAMS
        from .test_estimate_catalog import offer
        from pool_service.services.estimate_compatibility import repair
        params=[*PARAMS,('Загрузка гравия, кг','21')]
        large=[(k,'12' if k=='Расход воды, м³/ч' else v) for k,v in params]
        current=catalog.parse_catalog(feed(filtr(params=params),filtr(2,large,sku='F2'),offer(10,'Насос Test 12m3/h*8m 220V')))
        s=legacy(selected(current))
        catalog.apply_product(row(s,'filter'),current['products']['1'],current)
        catalog.apply_product(row(s,'pump'),current['products']['10'],current)
        s['inputs']['required_head']='8'
        before=engine.evaluate(s)
        self.assertTrue(row(before,'filter')['compatibility']['can_auto'])
        after=repair(before,'economy','equipment',row(before,'filter')['id'],current)
        self.assertEqual(len(rows(after)),1)
        for tier in ('standard','premium'):self.assertEqual(after['variants'][tier],before['variants'][tier])


class FilterGravelApiTests(TestCase):
    def setUp(self):
        self.org=Organization.objects.create(name='Synthetic gravel tenant')
        user=User.objects.create_user('gravel-test-owner')
        OrganizationAccess.objects.create(user=user,organization=self.org,role='owner')
        self.client.force_login(user);self.url=reverse('pool_estimate_api');self.data=owner_only()
        mocked=patch('pool_service.estimate_views.get_catalog',return_value=self.data)
        mocked.start();self.addCleanup(mocked.stop)

    def post(self,**data):
        response=self.client.post(self.url,json.dumps({'organization':self.org.pk,**data}),content_type='application/json')
        self.assertIn(response.status_code,(200,201),response.content[:500]);return response.json()

    def test_filter_apply_activates_only_selected_quote_and_history_preserves_manuals(self):
        old=legacy(selected(self.data))
        s=self.post(action='catalog_apply',snapshot=old,variant='economy',block='equipment',row='filter',product_id='1')['snapshot']
        self.assertEqual(len(rows(s)),1)
        for tier in ('standard','premium'):self.assertEqual(rows(s,tier=tier),[])
        rid=row(s,'filter_gravel')['id']
        s=self.post(action='catalog_apply',snapshot=s,variant='economy',block='equipment',row=rid,product_id='3')['snapshot']
        row(s,'filter_gravel').update(price='777',unit_price_manual=True,quantity='17',auto_quantity=False)
        saved=self.post(action='save',title='Synthetic gravel bags',snapshot=s,expected_revision=0)
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id']}).json()['snapshot']
        self.assertEqual((row(loaded,'filter_gravel')['price'],row(loaded,'filter_gravel')['quantity']),('777.00','17.000'))
        self.data['products']['3']['price']='999.00'
        validated=self.post(action='validate',snapshot=loaded)['snapshot']
        historical=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(loaded,historical)
        self.assertEqual(row(validated,'filter_gravel')['price'],'777.00')

    def test_old_glass_save_load_does_not_create_new_mass_charge(self):
        data=self.data;s=selected(data)
        catalog.apply_product(row(s),data['products']['4'],data,'manual')
        row(s).pop('media_sand_mass_fallback',None)
        saved=self.post(action='save',title='Synthetic historical unknown glass',snapshot=s,expected_revision=0)
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id']}).json()['snapshot']
        self.assertIsNone(row(loaded)['quantity']);self.assertIsNone(row(loaded)['amount'])
        loaded=self.post(action='validate',snapshot=loaded)['snapshot']
        self.assertIsNone(row(loaded)['quantity']);self.assertIsNone(row(loaded)['amount'])
