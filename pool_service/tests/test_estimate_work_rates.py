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
        self.assertEqual(block(fresh, 'finish', 'standard')['rows'][1]['price'], '800.00')
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
