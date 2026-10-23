"""Displayed operands determine charges; exact source values remain available."""
from copy import deepcopy
from decimal import Decimal as D
import json
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase
from django.template.loader import render_to_string
from django.urls import reverse
from pool_service.models import Organization, OrganizationAccess
from pool_service.estimate_models import PoolEstimate, PoolEstimateVersion
from pool_service.services import pool_estimates as engine, estimate_rounding as rounding
from pool_service.services.estimate_catalog import CatalogUnavailable, apply_product
from .test_pool_estimates import INPUTS
from .test_estimate_catalog import feed, offer


def example(quantity='15.576', price='20000', unit='м³'):
    s=engine.generate(INPUTS)
    for v in s['variants'].values():
        for b in v['blocks']:
            b['excluded']=b['id']!='options'
            if b['id']=='options':
                r=engine.make_row('rounding_test','Synthetic row',unit,quantity)
                r.update(auto_quantity=False,price=price,unit_price_manual=True)
                b['rows']=[r]
    return engine.evaluate(s)


def row(s,tier='economy'):
    return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['id']=='rounding_test')


class VisibleRoundingTests(SimpleTestCase):
    def test_visible_operands_all_three_quotes_source_and_repeated_evaluation(self):
        s=example()
        for tier in s['variants']:
            r=row(s,tier)
            self.assertEqual((r['quantity'],r['price']),('15.576','20000.00'))
            self.assertEqual((r['calculation_quantity'],r['calculation_price'],r['amount']),('15.6','20000','312000.00'))
            self.assertEqual(s['variants'][tier]['total'],'312000.00')
        self.assertEqual(engine.evaluate(s),s)
        changed=engine.recalculate(s,s['inputs'])
        self.assertEqual(row(changed)['amount'],'312000.00')
        self.assertEqual(row(changed)['quantity'],'15.576')

    def test_quantity_price_and_row_amount_half_up_including_pieces(self):
        for q,p,u,want in [('1.25','100.49','шт.','125.00'),('1.25','100.50','м²','131.00'),('.05','5','т','1.00'),('.04','5','т','0.00'),('1.234','166.06','м²','199.00')]:
            with self.subTest(q=q,p=p,u=u):
                s=example(q,p,u);r=row(s)
                self.assertEqual(r['amount'],want)
                self.assertEqual(r['price'],str(D(p).quantize(D('.01'))))
        for price,want in [('0','0.00'),(None,None),('123.45','1919.00')]:
            r=row(example(price=price));self.assertEqual(r['amount'],want)
            self.assertTrue(r['unit_price_manual'])

    def test_totals_sum_whole_row_amounts_exclusion_and_existing(self):
        s=example('1.25','1.49','шт.')
        for v in s['variants'].values():
            b=next(b for b in v['blocks'] if b['id']=='options')
            b['rows'].append({**deepcopy(b['rows'][0]),'id':'second'})
        s=engine.evaluate(s)
        for v in s['variants'].values():self.assertEqual(v['total'],'2.00')
        row(s)['excluded']=True;self.assertEqual(engine.evaluate(s)['variants']['economy']['total'],'1.00')
        row(s).update(excluded=False,existing=True);self.assertIsNone(row(engine.evaluate(s))['amount'])

    def test_roll_and_package_precision_cannot_override_visible_operands(self):
        from pool_service.services.estimate_catalog import parse_catalog
        from pool_service.services.estimate_geotextile import PREFERRED_URL
        products=parse_catalog(feed(
            offer(1,'Геотекстиль Aquaviva 300 г/м2 (50 м)','16606',url=PREFERRED_URL),
            offer(2,'Штукатурка Litokol CR30 (25 кг)','250.12',brand='Litokol'),
        ))
        for pid,kind in [('1','geotextile'),('2','plaster_component')]:
            s=example('1.234','0','м²' if pid=='1' else 'меш.');r=row(s)
            if pid=='2':r['basis']='plaster_mix_bags'
            apply_product(r,products['products'][pid],products)
            s=engine.evaluate(s);r=row(s)
            self.assertEqual(r['amount'],str((rounding.quantity(r['quantity'],r['unit'])*rounding.price(r['price'])).quantize(D('1')).quantize(D('.01'))))
            self.assertNotIn('без промежуточного округления',r.get('price_conversion_note',''))

    def test_historical_snapshot_stays_exact_until_explicit_recalculation(self):
        old=example();old.pop('visible_rounding');old=engine.evaluate(old)
        self.assertEqual(row(old)['amount'],'311520.00')
        before=deepcopy(old);self.assertEqual(engine.evaluate(old),before)
        changed=engine.recalculate(old,old['inputs'])
        self.assertEqual(row(changed)['amount'],'312000.00');self.assertEqual(old,before)

    def test_print_matches_displayed_values_without_mutating_snapshot(self):
        s=example();before=deepcopy(s)
        html=render_to_string('pool_service/estimates/print.html',{'version':{'snapshot':s,'number':1}})
        self.assertIn('15,6 м³',html);self.assertIn('>20000<',html);self.assertIn('>312000<',html)
        self.assertNotIn('311520',html);self.assertEqual(s,before)


class VisibleRoundingApiTests(TestCase):
    def test_saved_legacy_version_immutable_new_version_rounds_and_reloads(self):
        org=Organization.objects.create(name='Synthetic rounding tenant')
        user=User.objects.create_user('rounding-owner');OrganizationAccess.objects.create(user=user,organization=org,role='owner')
        self.client.force_login(user);url=reverse('pool_estimate_api')
        def post(**data):
            r=self.client.post(url,json.dumps({'organization':org.pk,**data}),content_type='application/json')
            self.assertIn(r.status_code,(200,201),r.content[:300]);return r.json()
        old=example();old.pop('visible_rounding');old=engine.evaluate(old)
        estimate=PoolEstimate.objects.create(organization=org,title=old['inputs']['title'],revision=1,created_by=user)
        PoolEstimateVersion.objects.create(estimate=estimate,number=1,snapshot=old,created_by=user)
        saved={'id':str(estimate.pk),'snapshot':old}
        with patch('pool_service.estimate_views.get_catalog',side_effect=CatalogUnavailable('Synthetic offline catalogue')):
            changed=post(action='recalculate',snapshot=saved['snapshot'],inputs=old['inputs'])['snapshot']
        self.assertEqual(row(changed)['amount'],'312000.00')
        post(action='save',snapshot=changed,id=saved['id'],expected_revision=1)
        history=self.client.get(url,{'organization':org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        current=self.client.get(url,{'organization':org.pk,'id':saved['id']}).json()['snapshot']
        self.assertEqual(row(history)['amount'],'311520.00');self.assertEqual(row(current)['amount'],'312000.00')
        self.assertEqual(history,saved['snapshot']);self.assertEqual(row(current)['quantity'],'15.576')
        edited=post(action='validate',snapshot=old)['snapshot']
        self.assertEqual(row(edited)['amount'],'312000.00')
