"""Synthetic rate memory: explicit confirmation, maximum, scopes and tenants."""
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
import json
from pool_service.models import Organization, OrganizationAccess
from pool_service.estimate_models import PoolEstimateWorkRate
from pool_service.services.pool_estimates import generate, evaluate
from pool_service.services.estimate_work_rates import confirm_price, apply_defaults
from pool_service.tests.test_pool_estimates import INPUTS, block


class WorkRateMemoryTests(TestCase):
    def test_equipment_base_is_common_and_only_missing_old_totals_differ(self):
        from .test_estimate_works import work
        fresh=generate({**INPUTS,'finish':'film','heating_types':['electric','exchanger','heatpump']})
        old=deepcopy(fresh)
        for tier in ('standard','premium'):
            work(old,'equipment_work',tier)['price']=None
        old=evaluate(old)
        for tier in fresh['variants']:
            row=work(fresh,'equipment_work',tier)
            self.assertEqual((row['price'],row['quantity'],row['amount']),('70000.00','1.000','70000.00'))
            self.assertEqual(sum(r.get('work_code')=='equipment_work' for b in fresh['variants'][tier]['blocks'] for r in b['rows']),1)
            delta=Decimal(fresh['variants'][tier]['total'])-Decimal(old['variants'][tier]['total'])
            self.assertEqual(delta,Decimal('0' if tier=='economy' else '70000'))
        self.assertEqual(evaluate(old)['variants'],old['variants'])

    def test_equipment_raise_shared_across_new_quotes_but_not_tenants_or_units(self):
        from .test_estimate_works import work
        from pool_service.services.estimate_work_rates import identity
        data={'snapshot':self.snapshot,'variant':'standard','block':'equipment',
              'row':'equipment_work','price':'90000'}
        result=confirm_price(data,self.org,self.user)
        self.assertEqual(result['remembered'],1)
        self.assertEqual(work(result['snapshot'],'equipment_work')['price'],'70000.00')
        before=list(PoolEstimateWorkRate.objects.values())
        for org,expected in ((self.org,'90000.00'),(self.other,'70000.00')):
            fresh=apply_defaults(generate(INPUTS),org)
            for tier in fresh['variants']:
                self.assertEqual(work(fresh,'equipment_work',tier)['price'],expected)
        self.assertEqual(list(PoolEstimateWorkRate.objects.values()),before)
        discount=confirm_price({**data,'snapshot':result['snapshot'],'price':'65000'},self.org,self.user)
        self.assertEqual(discount['remembered'],0)
        self.assertEqual(work(discount['snapshot'],'equipment_work','standard')['price'],'65000.00')
        unit_row=deepcopy(work(self.snapshot,'equipment_work'))
        unit_row['unit']='ч'
        self.assertNotEqual(identity(unit_row),identity(work(self.snapshot,'equipment_work')))
        from pool_service.services.estimate_works import shared_base
        self.assertIsNone(shared_base(unit_row))

    def test_equipment_manual_discount_zero_and_clear_survive_api_and_history(self):
        from .test_estimate_works import work
        from pool_service.services.estimate_catalog import CatalogUnavailable
        confirm_price({'snapshot':self.snapshot,'variant':'economy','block':'equipment',
                       'row':'equipment_work','price':'90000'},self.org,self.user)
        for tier,price in zip(self.snapshot['variants'],('65000','0',None)):
            work(self.snapshot,'equipment_work',tier).update(
                price=price,rate_source='Ручная цена текущей сметы')
        original=evaluate(self.snapshot)
        saved=self.post(action='save',snapshot=original,expected_revision=0)
        self.assertEqual(saved.status_code,201)
        records=list(PoolEstimateWorkRate.objects.values())
        with patch('pool_service.estimate_views.get_catalog',side_effect=CatalogUnavailable('Synthetic unavailable')):
            for action in ('validate','recalculate','add_works'):
                response=self.post(action=action,snapshot=original,inputs=original['inputs'])
                self.assertEqual(response.status_code,200)
                result=response.json()['snapshot']
                for tier in original['variants']:
                    self.assertEqual(work(result,'equipment_work',tier)['price'],work(original,'equipment_work',tier)['price'])
                    self.assertEqual(result['variants'][tier]['total'],original['variants'][tier]['total'])
        loaded=self.client.get(self.url,{'organization':self.org.pk,'id':saved.json()['id'],'version':1}).json()['snapshot']
        self.assertEqual(loaded['variants'],original['variants'])
        self.assertEqual(list(PoolEstimateWorkRate.objects.values()),records)

    def setUp(self):
        self.org = Organization.objects.create(name='Rate memory test')
        self.other = Organization.objects.create(name='Rate memory other')
        self.user = User.objects.create_user('rate-memory-test')
        OrganizationAccess.objects.create(organization=self.org, user=self.user, role='owner')
        self.client.force_login(self.user)
        self.snapshot = generate({**INPUTS, 'finish': 'film'})
        self.url = reverse('pool_estimate_api')

    def data(self, price, **kw):
        return {'snapshot': self.snapshot, 'variant': 'economy', 'block': 'finish', 'row': 'finish_work', 'price': price, **kw}

    def post(self, **data):
        return self.client.post(self.url, json.dumps({'organization': self.org.pk, **data}), content_type='application/json')

    def test_maximum_defaults_survive_discount_clear_and_stale_tab(self):
        frozen = deepcopy(self.snapshot)
        first = confirm_price(self.data('3000'), self.org, self.user)
        self.assertEqual(first['remembered'], 1)
        self.snapshot = first['snapshot']
        discounted = confirm_price(self.data('2500'), self.org, self.user)
        self.assertEqual(block(discounted['snapshot'], 'finish')['rows'][1]['price'], '2500.00')
        self.assertEqual(discounted['remembered'], 0)
        confirm_price(self.data(None), self.org, self.user)
        self.snapshot = frozen  # Older tab must not lower the maximum.
        confirm_price(self.data('2800'), self.org, self.user)
        fresh = apply_defaults(generate({**INPUTS, 'finish': 'film'}), self.org)
        self.assertEqual(block(fresh, 'finish')['rows'][1]['price'], '3000.00')
        self.assertEqual(block(fresh, 'finish', 'standard')['rows'][1]['price'], '3000.00')
        self.assertEqual(block(frozen, 'finish')['rows'][1]['price'], '2000.00')
        self.assertEqual(PoolEstimateWorkRate.objects.get().price, Decimal('3000'))

    def test_all_tiers_and_other_tenant_are_independent(self):
        confirm_price(self.data('3500', all=True), self.org, self.user)
        self.assertEqual(PoolEstimateWorkRate.objects.count(), 3)
        fresh = apply_defaults(generate({**INPUTS, 'finish': 'film'}), self.other)
        self.assertEqual(block(fresh, 'finish')['rows'][1]['price'], '2000.00')
        response = self.post(action='work_price', **self.data('9000'), organization=self.other.pk)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(PoolEstimateWorkRate.objects.filter(organization=self.other).count(), 0)

    def test_units_and_finishes_are_not_cross_applied(self):
        result = confirm_price({**self.data('3000', all=True), 'row': 'work_finish_transport'}, self.org, self.user)
        self.assertEqual(result['remembered'], 1)  # Others use package, not trip.
        confirm_price(self.data('4000', all=True), self.org, self.user)
        mosaic = apply_defaults(generate({**INPUTS, 'finish': 'mosaic'}), self.org)
        self.assertEqual(block(mosaic, 'finish', 'standard')['rows'][1]['price'], '2500.00')

    def test_draft_and_failed_validation_have_atomic_behavior(self):
        row = deepcopy(block(self.snapshot, 'finish')['rows'][1])
        row.update(price='3000', quantity='5', auto_quantity=False, name='Synthetic edited work')
        result = confirm_price(self.data('3000', all=True, draft_row=row), self.org, self.user)
        self.assertEqual(result['remembered'], 3)
        self.assertEqual(block(result['snapshot'], 'finish')['rows'][1]['quantity'], '5.000')
        self.assertEqual(block(result['snapshot'], 'finish', 'standard')['rows'][1]['quantity'], '68.000')
        before = list(PoolEstimateWorkRate.objects.values_list('price', flat=True))
        row['quantity'] = '-1'
        response = self.post(action='work_price', **self.data('9999', draft_row=row))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(PoolEstimateWorkRate.objects.values_list('price', flat=True)), before)

    def test_generate_uses_defaults_but_validate_save_and_recalculate_do_not_learn_prices(self):
        confirm_price(self.data('3000'), self.org, self.user)
        block(self.snapshot, 'finish')['rows'][1]['price'] = '9000'
        self.assertEqual(self.post(action='validate', snapshot=self.snapshot).status_code, 200)
        saved = self.post(action='save', snapshot=self.snapshot, expected_revision=0)
        self.assertEqual(saved.status_code, 201)
        from pool_service.tests.test_estimate_catalog import fixture
        with patch('pool_service.estimate_views.get_catalog', return_value=fixture()):
            generated = self.post(action='generate', inputs={**INPUTS, 'finish': 'film'}).json()['snapshot']
            recalculated = self.post(action='recalculate', snapshot=self.snapshot, inputs={**INPUTS, 'finish':'film'}).json()['snapshot']
        self.assertEqual(block(generated, 'finish')['rows'][1]['price'], '3000.00')
        self.assertEqual(block(recalculated, 'finish')['rows'][1]['price'], '9000.00')
        self.assertEqual(PoolEstimateWorkRate.objects.get().price, Decimal('3000'))
        old = self.client.get(self.url, {'organization':self.org.pk,'id':saved.json()['id'],'version':1}).json()['snapshot']
        self.assertEqual(block(old, 'finish')['rows'][1]['price'], '9000.00')

    def test_lower_than_previous_quote_rate_is_not_remembered(self):
        result = confirm_price(self.data('1500'), self.org, self.user)
        self.assertEqual(result['remembered'], 0)
        self.assertFalse(PoolEstimateWorkRate.objects.exists())

    def test_shared_film_bases_and_conflicting_records_read_max_without_migration(self):
        from pool_service.services.estimate_work_rates import identity
        base=generate({**INPUTS,'finish':'film'})
        for variant in base['variants']:
            self.assertEqual(block(base,'finish',variant)['rows'][1]['price'],'2000.00')
            underlay=next(r for r in block(base,'finish',variant)['rows'] if r.get('work_code')=='underlay_film')
            self.assertEqual(underlay['price'],'200.00')
        target=block(base,'finish')['rows'][1]
        for variant,price in [('economy','2500'),('standard','3100'),('premium','2700')]:
            PoolEstimateWorkRate.objects.create(organization=self.org,variant=variant,identity=identity(target),work_code='finish_film',unit='м²',finish_type='film',price=price)
        before=list(PoolEstimateWorkRate.objects.values())
        fresh=apply_defaults(deepcopy(base),self.org)
        for variant in fresh['variants']:
            r=block(fresh,'finish',variant)['rows'][1]
            self.assertEqual(r['price'],'3100.00')
            self.assertIn('Стандарт',r['rate_source'])
        self.assertEqual(list(PoolEstimateWorkRate.objects.values()),before)
        self.assertEqual(block(apply_defaults(deepcopy(base),self.other),'finish')['rows'][1]['price'],'2000.00')
        self.snapshot=fresh
        self.assertEqual(confirm_price(self.data('3000'),self.org,self.user)['remembered'],0)
        self.assertEqual(list(PoolEstimateWorkRate.objects.values()),before)
        protected={(v,r['id']) for v,b in fresh['variants'].items() for block_ in b['blocks'] for r in block_['rows']}
        block(fresh,'finish')['rows'][1]['price']='1234'
        self.assertEqual(block(apply_defaults(fresh,self.org,only_new=protected),'finish')['rows'][1]['price'],'1234.00')

    def test_geotextile_raise_shared_but_below_base_not_remembered(self):
        data={**self.data('250'),'row':'work_underlay'}
        result=confirm_price(data,self.org,self.user)
        self.assertEqual(result['remembered'],1)
        fresh=apply_defaults(generate({**INPUTS,'finish':'film'}),self.org)
        for variant in fresh['variants']:
            r=next(r for r in block(fresh,'finish',variant)['rows'] if r.get('work_code')=='underlay_film')
            self.assertEqual(r['price'],'250.00')
        self.snapshot=result['snapshot']
        confirm_price({**data,'snapshot':self.snapshot,'price':'100'},self.org,self.user)
        self.assertEqual(PoolEstimateWorkRate.objects.get().price,Decimal('250'))

    def test_old_film_prices_survive_recalculate_save_and_history(self):
        from pool_service.services.pool_estimates import recalculate
        snapshot=generate({**INPUTS,'finish':'film'})
        for variant in ('standard','premium'):
            for row in block(snapshot,'finish',variant)['rows']:
                if row.get('work_code')=='finish_film': row['price']='800'
                if row.get('work_code')=='underlay_film': row['price']='70'
        snapshot=evaluate(snapshot)
        recalculated=recalculate(snapshot,snapshot['inputs'])
        for variant in snapshot['variants']:
            self.assertEqual(snapshot['variants'][variant]['total'],recalculated['variants'][variant]['total'])
        saved=self.post(action='save',snapshot=snapshot,expected_revision=0).json()
        old=self.client.get(self.url,{'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertEqual(old['variants'],snapshot['variants'])
        new=generate({**INPUTS,'finish':'film'})
        for variant in ('standard','premium'):
            self.assertEqual(Decimal(new['variants'][variant]['total'])-Decimal(snapshot['variants'][variant]['total']),Decimal('90440'))
        self.assertEqual(new['variants']['economy']['total'],snapshot['variants']['economy']['total'])

    def test_legacy_add_works_endpoint_preserves_explicit_clear_with_memory(self):
        self.snapshot=confirm_price(self.data('3000'),self.org,self.user)['snapshot']
        cleared=confirm_price(self.data(None),self.org,self.user)['snapshot']
        for action in ('add_works','recalculate'):
            data={'action':action,'snapshot':cleared}
            if action=='recalculate': data['inputs']=cleared['inputs']
            from pool_service.services.estimate_catalog import CatalogUnavailable
            with patch('pool_service.estimate_views.get_catalog',side_effect=CatalogUnavailable('Synthetic unavailable')):
                response=self.post(**data)
            self.assertEqual(response.status_code,200)
            self.assertIsNone(block(response.json()['snapshot'],'finish')['rows'][1]['price'])
        self.assertEqual(PoolEstimateWorkRate.objects.get().price,Decimal('3000'))
