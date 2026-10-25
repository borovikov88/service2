from copy import deepcopy
import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from django.contrib.auth.models import User
from django.urls import reverse

from pool_service.models import Organization, OrganizationAccess
from pool_service.services.pool_estimates import generate, evaluate, EstimateInputError
from pool_service.services.estimate_compatibility import repair
from pool_service.services import estimate_catalog as catalog, estimate_works as works
from pool_service.services.estimate_work_rates import confirm_price
from pool_service.tests.test_pool_estimates import INPUTS, block
from pool_service.tests.test_estimate_skimmer import fitting
from pool_service.tests.test_estimate_catalog import offer, feed
from pool_service.tests.test_estimate_works import work


def stock():
    return catalog.parse_catalog(feed(
        offer(1, 'Насос Aquaviva Test (220V, 20m3/h*8m)', price='1000'),
        offer(2, 'Фильтр Aquaviva Small (10 м3/ч)', price='100'),
        offer(3, 'Фильтр Aquaviva Enough (20 м3/ч)', price='500'),
        offer(4, 'Фильтр Aquaviva Luxury (25 м3/ч)', price='900'),
        offer(5, 'Фильтр Other Giant (100 м3/ч)', brand='Other', price='9999'),
    ))


def mismatched():
    s = generate({**INPUTS, 'finish': 'film', 'required_head': '8'})
    c = stock()
    catalog.apply_product(block(s)['rows'][0], c['products']['1'], c)
    catalog.apply_product(block(s)['rows'][1], c['products']['2'], c)
    for role in ('skimmer', 'inlet'):
        fitting(s, role).update(auto_quantity=False, quantity='1', price='100')
    return evaluate(s)


class CompatibilityTests(SimpleTestCase):
    def test_warnings_and_one_row_repair_preserve_other_quotes_and_prices(self):
        s = mismatched(); original = deepcopy(s)
        for row in [block(s)['rows'][1], fitting(s, 'skimmer'), fitting(s, 'inlet')]:
            self.assertTrue(row['compatibility']['can_auto'])
        result = repair(s, 'economy', 'equipment', 'filter', stock())
        self.assertEqual(block(result)['rows'][1]['catalog']['id'], '3')
        # The physical undersizing is repaired; legacy title-only catalogue
        # evidence still requires card/completeness verification.
        self.assertFalse(block(result)['rows'][1]['compatibility']['can_auto'])
        self.assertIn('характеристики фильтра', block(result)['rows'][1]['selection_note'])
        self.assertEqual(block(result)['rows'][0], block(s)['rows'][0])
        self.assertEqual(fitting(result, 'inlet'), fitting(s, 'inlet'))
        for key in ('standard', 'premium'):
            self.assertEqual(result['variants'][key], s['variants'][key])
        for role, expected in (('skimmer', '4.000'), ('inlet', '8.000')):
            before = fitting(result, role)
            result = repair(result, 'economy', 'embedded', before['id'])
            self.assertEqual(fitting(result, role)['quantity'], expected)
            if role == 'inlet':
                self.assertEqual(fitting(result, role)['filtration_quantity'], '7.000')
                self.assertEqual(fitting(result, role)['level_quantity'], '1.000')
            self.assertEqual(fitting(result, role)['price'], '100.00')
            self.assertIsNone(fitting(result, role)['compatibility'])
        self.assertEqual(s, original)

    def test_existing_rows_contribute_but_cannot_be_changed_or_restored(self):
        s = mismatched()
        new = deepcopy(fitting(s, 'skimmer'))
        new.update(id='existing_skimmer', existing=True, excluded=True, quantity='2')
        block(s, 'embedded')['rows'].append(new)
        s = evaluate(s)
        self.assertFalse(next(r for r in block(s, 'embedded')['rows'] if r['id']=='existing_skimmer')['compatibility']['can_auto'])
        result = repair(s, 'economy', 'embedded', fitting(s, 'skimmer')['id'])
        self.assertEqual(fitting(result, 'skimmer')['quantity'], '2.000')
        self.assertTrue(next(r for r in block(result, 'embedded')['rows'] if r['id']=='existing_skimmer')['excluded'])
        block(s)['rows'][1]['existing'] = True
        with self.assertRaises(EstimateInputError): repair(s, 'economy', 'equipment', 'filter', stock())
        block(s, 'embedded')['excluded'] = True
        with self.assertRaises(EstimateInputError): repair(s, 'economy', 'embedded', fitting(s, 'skimmer')['id'])

    def test_unknown_or_multi_pump_never_guesses_and_failed_choice_preserves_input(self):
        for mode in ('unknown', 'multi', 'unit'):
            s = mismatched()
            pump = block(s)['rows'][0]
            if mode == 'unknown': pump.update(catalog=None, specs={})
            elif mode == 'multi': pump['quantity']='2'
            else: pump['unit']='м²'
            result = evaluate(s)
            self.assertFalse(block(result)['rows'][1]['compatibility']['can_auto'])
            self.assertFalse(fitting(result, 'inlet')['compatibility']['can_auto'])
            with self.assertRaises(EstimateInputError): repair(s, 'economy', 'equipment', 'filter', stock())
        s = mismatched(); original = deepcopy(s)
        with self.assertRaises(EstimateInputError): repair(s, 'economy', 'equipment', 'filter', {'products': {}})
        self.assertEqual(s, original)

    def test_unknown_head_is_preliminary_and_premium_does_not_select_giant_filter(self):
        s = mismatched(); s['inputs']['required_head'] = None
        s['variants']['premium'] = deepcopy(s['variants']['economy'])
        s = evaluate(s)
        self.assertIn('Предварительно', block(s)['rows'][1]['compatibility']['message'])
        result = repair(s, 'premium', 'equipment', 'filter', stock())
        self.assertEqual(block(result, variant='premium')['rows'][1]['catalog']['id'], '4')

    def test_source_rate_fill_is_explicit_blank_only_and_unit_safe(self):
        s = generate({**INPUTS, 'finish': 'film'})
        for key in s['variants']:
            self.assertEqual(work(s, 'primer', key)['price'], '70.00')
            self.assertEqual(work(s, 'sand_base', key)['price'], '200.00')
        work(s, 'primer')['price'] = None
        work(s, 'plaster')['price'] = '0'
        work(s, 'sand_base').update(price=None, unit='компл.')
        self.assertIsNone(work(evaluate(s), 'primer')['price'])
        works.fill_missing_prices(s)
        self.assertEqual(work(s, 'primer')['price'], '70')
        self.assertEqual(work(s, 'plaster')['price'], '0')
        self.assertIsNone(work(s, 'sand_base')['price'])
        self.assertIsNone(work(s, 'excavation')['price'])
        self.assertEqual(work(s, 'equipment_work', 'standard')['price'], '70000.00')


class CompatibilityApiTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Compatibility test')
        self.other = Organization.objects.create(name='Other compatibility test')
        self.user = User.objects.create_user('compatibility-owner')
        OrganizationAccess.objects.create(user=self.user, organization=self.org, role='owner')
        self.client.force_login(self.user)
        self.url = reverse('pool_estimate_api')

    def post(self, **data):
        return self.client.post(self.url, json.dumps({'organization': self.org.pk, **data}), content_type='application/json')

    def test_tenant_guard_and_forged_repair_permission(self):
        s = mismatched()
        request = dict(action='compatibility_auto', snapshot=s, variant='economy', block='equipment', row='filter')
        self.assertEqual(self.post(**request, organization=self.other.pk).status_code, 403)
        with patch('pool_service.estimate_views.get_catalog', return_value=stock()):
            self.assertEqual(self.post(**request).status_code, 200)
            block(s)['rows'][1]['existing'] = True
            self.assertEqual(self.post(**request).status_code, 400)
        # Quantity correction does not require a network call to the shop.
        with patch('pool_service.estimate_views.get_catalog', side_effect=AssertionError('No network')):
            self.assertEqual(self.post(action='compatibility_auto', snapshot=s, variant='economy', block='embedded', row=fitting(s,'inlet')['id']).status_code, 200)

    def test_excel_fill_respects_remembered_rate_existing_values_and_saved_version(self):
        s = generate({**INPUTS, 'finish': 'film'})
        confirm_price({'snapshot':s, 'variant':'economy','block':'finish','row':'work_primer','price':'150','all':False}, self.org, self.user)
        work(s, 'primer')['price'] = None
        work(s, 'primer', 'standard')['price'] = '35'
        saved = self.post(action='save', snapshot=s, expected_revision=0).json()
        result = self.post(action='add_works', snapshot=s)
        self.assertEqual(result.status_code, 200)
        updated = result.json()['snapshot']
        self.assertEqual(work(updated, 'primer')['price'], '150.00')
        self.assertEqual(work(updated, 'primer', 'standard')['price'], '35.00')
        old = self.client.get(self.url, {'organization':self.org.pk,'id':saved['id'],'version':1}).json()['snapshot']
        self.assertIsNone(work(old, 'primer')['price'])
