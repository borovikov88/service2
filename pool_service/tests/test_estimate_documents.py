"""Customer exports, not supplier price data. One endpoint/tenant regression suite."""
from copy import deepcopy
import json
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase, SimpleTestCase
from django.template.loader import render_to_string
from django.urls import reverse
from pool_service.models import Organization, OrganizationAccess
from pool_service.estimate_models import PoolEstimate, PoolEstimateVersion, PoolEstimateDocumentSettings
from pool_service.services import pool_estimates as engine, estimate_documents as docs
from .test_pool_estimates import INPUTS

OPTIONS={'client_name':'Synthetic customer','object_address':'Synthetic project address','proposal_number':'TEST-01','proposal_date':'2026-10-23','variants':['economy','standard'],'types':['estimate','equipment'],'technical_reviewed':True,'equipment_warranty':'Test equipment terms','installation_warranty':'Test installation terms'}


def fixture():
    s=engine.generate(INPUTS);s['documents']=docs.normalize(OPTIONS)
    for tier,v in s['variants'].items():
        for b in v['blocks']:
            b['excluded']=b['id']!='equipment'
            if b['id']=='equipment':
                r=engine.make_row('test_pump','PREMIUM-SECRET' if tier=='premium' else 'Test filtration pump','шт.','1',kind='pump')
                r.update(model='Model-P',price='10000',auto_quantity=False,selection_note='INTERNAL-CHECK-SECRET',brand='INTERNAL-BRAND-SECRET')
                b['rows']=[r]
    return engine.evaluate(s)


class DocumentTests(SimpleTestCase):
    def test_selected_variants_clean_print_and_comparison_no_snapshot_mutation(self):
        s=fixture();before=deepcopy(s);payload=docs.build(s,s['documents'])
        self.assertEqual([v['key'] for v in payload['variants']],['economy','standard'])
        self.assertEqual(len(payload['cards']),1);self.assertEqual(payload['comparison'],[])
        self.assertEqual(docs.readiness(s,s['documents']),[])
        for kind in ['estimate','equipment']:
            html=render_to_string('pool_service/estimates/document.html',{'doc':payload,'document_kind':kind,'estimate':{'title':'Test','pk':'test-id','organization':{'name':'Test company'}},'version':{'number':1}})
            for hidden in ['PREMIUM-SECRET','INTERNAL-CHECK-SECRET','INTERNAL-BRAND-SECRET','арт.','Текущая выгрузка']:
                self.assertNotIn(hidden,html)
            self.assertIn('Synthetic customer',html);self.assertIn('Synthetic project address',html)
            self.assertIn('Test equipment terms',html);self.assertIn('10000',html)
        self.assertEqual(s,before)

    def test_unknown_prices_not_zero_and_unselected_missing_premium_does_not_block(self):
        s=fixture();next(b for b in s['variants']['premium']['blocks'] if b['id']=='equipment')['rows'][0]['price']=None
        self.assertEqual(docs.readiness(s,s['documents']),[])
        target=next(b for b in s['variants']['economy']['blocks'] if b['id']=='equipment')['rows'][0]
        target['price']=None
        self.assertTrue(any('уточните' in e for e in docs.readiness(s,s['documents'])))
        target['excluded']=True
        self.assertTrue(any('нет позиций' in e for e in docs.readiness(s,s['documents'])))
        for values in [{'variants':[]},{'variants':['economy','economy']},{'variants':['other']},{'types':['other']},{'proposal_date':'2026-99-99'}]:
            with self.subTest(values=values):
                with self.assertRaises(engine.EstimateInputError):docs.normalize(values)

    def test_three_columns_safe_images_and_only_confirmed_features(self):
        s=fixture();options=docs.normalize({**OPTIONS,'variants':['economy','standard','premium']})
        result=docs.build(s,options);self.assertTrue(result['landscape'])
        self.assertEqual(len(result['variants']),3);self.assertEqual(len(result['comparison'][0]['cells']),3)
        self.assertEqual(result['cards'],[]);self.assertEqual(result['comparison'][0]['cells'][0][0]['image'],'')
        light=engine.make_row('test_light','Synthetic pool light','шт.','2',kind='pool_light')
        light.update(price='100',amount='200',catalog={'id':'light1','image':'https://evil.invalid/private.png'})
        next(b for b in s['variants']['economy']['blocks'] if b['id']=='equipment')['rows'].append(light)
        result=docs.build(s,options)
        self.assertTrue(any(c['label']=='Освещение' for c in result['comparison']))
        self.assertEqual(next(c for c in result['comparison'] if c['label']=='Освещение')['cells'][0][0]['image'],'')
        self.assertNotIn('надёж',str(result));self.assertNotIn('экономич',str(result))


class ComparisonDifferenceTests(SimpleTestCase):
    def test_differences_have_photos_and_saved_costs_common_cards_have_no_prices(self):
        s=fixture()
        for key in ('economy','standard'):
            rows=next(b for b in s['variants'][key]['blocks'] if b['id']=='equipment')['rows']
            rows[0].update(name='Pump '+key,price='10000' if key=='economy' else '12000')
            common=engine.make_row('common_filter','Shared filter','шт.','1',kind='filter')
            common.update(price='500',auto_quantity=False)
            rows.append(common)
        s=engine.evaluate(s)
        # Keep this display-only fixture to its two specified goods.
        for key in ('economy','standard'):
            block=next(b for b in s['variants'][key]['blocks'] if b['id']=='equipment')
            block['rows']=[r for r in block['rows'] if r['id'] in {'test_pump','common_filter'}]
        for key in ('economy','standard'):
            rows=next(b for b in s['variants'][key]['blocks'] if b['id']=='equipment')['rows']
            rows[0]['catalog']={'id':key,'image':'https://shop.aqualine22.ru/image/catalog/test-'+key+'.png'}
        before=deepcopy(s);result=docs.build(s,s['documents'])
        self.assertEqual([x['label'] for x in result['comparison']],['Фильтрационный насос'])
        cells=result['comparison'][0]['cells']
        self.assertEqual([items[0]['amount'] for items in cells],['10000.00','12000.00'])
        self.assertTrue(all(items[0]['image'] for items in cells))
        self.assertEqual([card['name'] for card in result['cards']],['Shared filter'])
        self.assertTrue(all('price' not in card and 'amount' not in card for card in result['cards']))
        self.assertEqual([v['total'] for v in result['variants']],['10500.00','12500.00'])
        html=render_to_string('pool_service/estimates/document.html',{'doc':result,'document_kind':'equipment','estimate':{'pk':'test','title':'Test'},'version':{'number':1}})
        comparison=html.split('<section class="equipment-comparison">')[1].split('</section>',1)[0]
        common=html.split('<section class="equipment-description">')[1].split('</section>',1)[0]
        self.assertIn('test-economy.png',comparison);self.assertIn('Стоимость: 12000',comparison)
        self.assertNotIn('Shared filter',comparison);self.assertNotIn('₽',common)
        self.assertIn('Shared filter',common);self.assertIn('12500',comparison)
        self.assertEqual(s,before)

    def test_same_model_with_other_quantity_or_visible_price_is_a_difference(self):
        for changed in ({'quantity':'2'},{'price':'10001'}):
            s=fixture();r=next(b for b in s['variants']['standard']['blocks'] if b['id']=='equipment')['rows'][0]
            r.update(changed);s=engine.evaluate(s);result=docs.build(s,s['documents'])
            self.assertEqual(len(result['comparison']),1);self.assertEqual(result['cards'],[])
        s=fixture()
        for key,price in [('economy','10000.01'),('standard','10000.49')]:
            next(b for b in s['variants'][key]['blocks'] if b['id']=='equipment')['rows'][0]['price']=price
        s=engine.evaluate(s);self.assertEqual(docs.build(s,s['documents'])['comparison'],[])

    def test_shared_items_removed_individually_without_losing_duplicate_rows(self):
        s=fixture()
        for key in ('economy','standard'):
            rows=next(b for b in s['variants'][key]['blocks'] if b['id']=='equipment')['rows']
            rows.append({**deepcopy(rows[0]),'id':'extra_pump','name':'Additional '+key,'price':'200','amount':'200.00'})
        result=docs.build(s,s['documents'])
        self.assertEqual([len(cell) for cell in result['comparison'][0]['cells']],[1,1])
        self.assertTrue(all('Additional' in cell[0]['name'] for cell in result['comparison'][0]['cells']))
        self.assertEqual(len(result['cards']),1)
        s=fixture();rows=next(b for b in s['variants']['economy']['blocks'] if b['id']=='equipment')['rows']
        rows.append({**deepcopy(rows[0]),'id':'duplicate'})
        result=docs.build(s,s['documents']);self.assertEqual([len(cell) for cell in result['comparison'][0]['cells']],[1,0])

    def test_selection_only_and_one_quote_moves_all_goods_below_table(self):
        s=fixture()
        two=docs.build(s,s['documents']);self.assertEqual(two['comparison'],[])
        three=docs.build(s,docs.normalize({**OPTIONS,'variants':['economy','standard','premium']}))
        self.assertEqual(len(three['comparison']),1);self.assertEqual(three['cards'],[])
        one=docs.build(s,docs.normalize({**OPTIONS,'variants':['economy']}))
        self.assertEqual(one['comparison'],[]);self.assertEqual(len(one['cards']),1)


class DocumentApiTests(TestCase):
    def setUp(self):
        self.org=Organization.objects.create(name='Synthetic document company')
        self.other=Organization.objects.create(name='Other synthetic document company')
        self.user=User.objects.create_user('document-owner');OrganizationAccess.objects.create(user=self.user,organization=self.org,role='owner')
        self.client.force_login(self.user);self.api=reverse('pool_estimate_api')
        self.estimate=PoolEstimate.objects.create(organization=self.org,title='Test',revision=1,created_by=self.user)
        self.snapshot=fixture();PoolEstimateVersion.objects.create(estimate=self.estimate,number=1,snapshot=self.snapshot,created_by=self.user)
        self.url=reverse('pool_estimate_document',args=[self.estimate.pk,1])

    def post(self,**values):
        return self.client.post(self.api,json.dumps({'organization':self.org.pk,**values}),content_type='application/json')

    def test_exports_are_frozen_and_tenant_protected_and_reject_bad_selection(self):
        with patch('pool_service.estimate_views.get_catalog',side_effect=AssertionError('Documents must not fetch catalogue')):
            for kind in ['estimate','equipment']:
                r=self.client.get(self.url,{'document':kind,'variants':'economy,standard'})
                self.assertEqual(r.status_code,200);self.assertContains(r,'Synthetic customer')
                self.assertNotContains(r,'PREMIUM-SECRET');self.assertNotContains(r,'INTERNAL-CHECK-SECRET')
            self.assertEqual(self.client.get(self.url,{'variants':'unknown'}).status_code,400)
        self.assertEqual(self.estimate.versions.get(number=1).snapshot,self.snapshot)
        stranger=User.objects.create_user('document-stranger');OrganizationAccess.objects.create(user=stranger,organization=self.other,role='owner')
        self.client.force_login(stranger);self.assertEqual(self.client.get(self.url).status_code,404)
        self.client.logout();self.assertEqual(self.client.get(self.url).status_code,302)

    def test_readiness_guards_save_no_prices_not_silently_zero_and_metadata_roundtrip(self):
        s=deepcopy(self.snapshot)
        r=next(b for b in s['variants']['economy']['blocks'] if b['id']=='equipment')['rows'][0];r['price']=None
        checked=self.post(action='document_check',snapshot=s);self.assertTrue(checked.json()['errors'])
        rejected=self.post(action='save',snapshot=s,expected_revision=0,for_documents=True)
        self.assertEqual(rejected.status_code,400);self.assertEqual(PoolEstimate.objects.filter(organization=self.org).count(),1)
        saved=self.post(action='save',snapshot=self.snapshot,expected_revision=0,for_documents=True)
        self.assertEqual(saved.status_code,201,saved.content[:300])
        data=saved.json();loaded=self.client.get(self.api,{'organization':self.org.pk,'id':data['id']}).json()['snapshot']
        self.assertEqual(loaded['documents'],self.snapshot['documents'])
        self.assertRedirects(self.client.get(reverse('pool_estimate_print',args=[data['id'],1])),reverse('pool_estimate_document',args=[data['id'],1]),fetch_redirect_response=False)
        html=self.client.get(reverse('pool_estimate_document',args=[data['id'],1])).content.decode()
        self.assertNotIn('INTERNAL-BRAND-SECRET',html)

    def test_company_defaults_owner_only_and_do_not_store_client_data(self):
        r=self.post(action='document_defaults',values=OPTIONS);self.assertEqual(r.status_code,200)
        stored=PoolEstimateDocumentSettings.objects.get(organization=self.org)
        self.assertNotIn('client_name',stored.values);self.assertNotIn('object_address',stored.values)
        self.assertFalse(PoolEstimateDocumentSettings.objects.filter(organization=self.other).exists())
        manager=User.objects.create_user('document-manager');OrganizationAccess.objects.create(user=manager,organization=self.org,role='manager')
        self.client.force_login(manager)
        self.assertEqual(self.post(action='document_defaults',values=OPTIONS).status_code,403)
        self.assertEqual(self.client.get(self.url).status_code,200)
        html=self.client.get(reverse('pool_estimate_page')).content.decode()
        self.assertNotIn('id="pe-document-defaults-save"',html)

    def test_old_versions_not_silently_repriced_or_exported_as_new(self):
        self.snapshot.pop('visible_rounding');self.snapshot.pop('documents')
        old=PoolEstimateVersion.objects.create(estimate=self.estimate,number=2,snapshot=self.snapshot,created_by=self.user)
        r=self.client.get(reverse('pool_estimate_document',args=[self.estimate.pk,2]))
        self.assertContains(r,'Пересчитайте параметры');self.assertNotContains(r,'class="quote-table"')
        self.assertEqual(PoolEstimateVersion.objects.get(pk=old.pk).snapshot,self.snapshot)
