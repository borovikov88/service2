"""Synthetic public availability summaries; no live stock or supplier values."""
from copy import deepcopy
from decimal import Decimal
import json

from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import generate, evaluate, recalculate
from .test_estimate_catalog import offer, feed
from .test_pool_estimates import INPUTS, block


def product(pid=1, state='own_stock', *, price='100', orderable='true', label='', extra=()):
    xml = offer(pid, 'Насос Test 8m3/h*8m 220V', price=price, available=orderable)
    xml = xml.replace('<param name="Наличие для подбора">own_stock</param>', '')
    params = [] if state is None else [('Наличие для подбора', state)]
    if label:
        params.append(('Наличие', label))
    from xml.sax.saxutils import escape
    return xml.replace('</offer>', ''.join('<param name="' + escape(k) + '">' + escape(v) + '</param>' for k, v in params + list(extra)) + '</offer>')


def row(snapshot, tier='economy'):
    return next(r for r in block(snapshot, 'equipment', tier)['rows'] if r['kind'] == 'pump')


class AvailabilityTests(SimpleTestCase):
    def test_orderable_does_not_confirm_own_or_supplier_stock(self):
        for state in ['preorder', 'unavailable', 'unknown', None, 'new_unknown_value']:
            c = catalog.parse_catalog(feed(product(state=state, label='Под заказ')))
            p = c['products']['1']
            self.assertFalse(p['available'], state)
            self.assertTrue(p['availability']['orderable'])
            result, _ = catalog.auto_select(generate(INPUTS), c)
            for tier in result['variants']:
                self.assertIsNone(row(result, tier)['catalog'], state)
            self.assertIn('налич', row(result)['selection_note'].casefold())

    def test_confirmed_supplier_and_own_stock_are_eligible_but_cannot_override_order_block(self):
        for state in ['own_stock', 'supplier_stock']:
            c = catalog.parse_catalog(feed(product(state=state)))
            self.assertTrue(c['products']['1']['available'])
            selected, _ = catalog.auto_select(generate(INPUTS), c)
            self.assertEqual(row(selected)['catalog']['availability']['state'], state)
            p = catalog.parse_catalog(feed(product(state=state, orderable='false')))['products']['1']
            self.assertFalse(p['available'])
            self.assertIn('заказ', catalog.candidate_reason(p, INPUTS, Decimal('8')))

    def test_premium_cannot_pick_expensive_backorder_over_confirmed_stock(self):
        c = catalog.parse_catalog(feed(product(1, 'supplier_stock', price='100'),
                                      product(2, 'preorder', price='10000', label='Под заказ')))
        result, _ = catalog.auto_select(generate(INPUTS), c)
        for tier in result['variants']:
            self.assertEqual(row(result, tier)['catalog']['id'], '1')
        search = catalog.search_catalog(c, '', 'pump', '', INPUTS, Decimal('8'))
        backorder = next(p for p in search['products'] if p['id'] == '2')
        self.assertIn('под заказ', backorder['warning'].casefold())

    def test_repeated_unknown_conflicting_and_equal_public_summaries(self):
        for extra in [(('Наличие для подбора', 'preorder'),),
                      (('Наличие для подбора', 'unexpected'),),
                      (('Наличие', 'В наличии'), ('Наличие', 'Под заказ'))]:
            p = catalog.parse_catalog(feed(product(extra=extra)))['products']['1']
            self.assertFalse(p['available'])
            self.assertTrue(p['availability']['issue'])
        p = catalog.parse_catalog(feed(product(extra=(('Наличие для подбора', 'own_stock'),))))['products']['1']
        self.assertTrue(p['available'])

    def test_refresh_replaces_automatic_candidate_and_preserves_manual_price_quantity_and_history(self):
        initial_catalog = catalog.parse_catalog(feed(product(1, price='10000'), product(2, price='100')))
        initial, _ = catalog.auto_select(generate(INPUTS), initial_catalog)
        self.assertEqual(row(initial, 'premium')['catalog']['id'], '1')
        old = deepcopy(initial)
        current = catalog.parse_catalog(feed(product(1, 'preorder', price='11000'), product(2, price='150')))
        refreshed, _ = catalog.auto_select(initial, current)
        self.assertEqual(row(refreshed, 'premium')['catalog']['id'], '2')
        self.assertEqual(initial, old)
        protected = deepcopy(initial)
        manual = row(protected, 'premium')
        manual['catalog']['selection'] = 'manual'
        manual.update(price='777', quantity='2')
        result, _ = catalog.auto_select(protected, current)
        actual = row(result, 'premium')
        self.assertEqual(actual['catalog']['id'], '1')
        self.assertEqual(Decimal(actual['price']), Decimal('777'))
        self.assertEqual(Decimal(actual['quantity']), Decimal('2'))
        self.assertIn('под заказ', actual['catalog_review_note'].casefold())
        self.assertEqual(row(evaluate(json.loads(json.dumps(result))), 'premium')['catalog']['id'], '1')
        self.assertEqual(row(recalculate(result, INPUTS), 'premium')['catalog']['id'], '1')

    def test_stock_summary_survives_save_load_and_explicit_price_refresh(self):
        c = catalog.parse_catalog(feed(product(label='В наличии')))
        s, _ = catalog.auto_select(generate(INPUTS), c)
        loaded = evaluate(json.loads(json.dumps(s)))
        self.assertEqual(row(loaded)['catalog']['availability'], c['products']['1']['availability'])
        updated = catalog.parse_catalog(feed(product(state='preorder', price='200', label='Под заказ')))
        refreshed, _, _ = catalog.refresh_prices(loaded, updated, 'economy')
        self.assertFalse(row(refreshed)['catalog']['available'])
        self.assertEqual(row(refreshed)['catalog']['availability']['state'], 'preorder')
        self.assertTrue(any('под заказ' in w.casefold() for w in refreshed['variants']['economy']['warnings']))

    def test_frozen_noncore_selections_refresh_only_stock_without_changing_price_date(self):
        from .test_estimate_liner import stock, film
        initial_catalog = stock()
        initial, _ = catalog.auto_select(generate({**INPUTS, 'finish': 'film'}), initial_catalog)
        current = deepcopy(initial_catalog)
        current.update(checked_at='2026-10-25T12:00:00+00:00', feed_date='2026-10-25 12:00')
        current['products']['1'].update(price='999999.00', available=False,
            availability={'state': 'preorder', 'label': 'Под заказ', 'issue': '', 'orderable': True})
        for manual in (False, True):
            saved = deepcopy(initial)
            if manual:
                film(saved)['catalog']['selection'] = 'manual'
                film(saved).update(price='777', quantity='2', auto_quantity=False, unit_price_manual=True)
            before = deepcopy(saved)
            refreshed, _ = catalog.auto_select(saved, current, only_variant='economy')
            actual, previous = film(refreshed), film(before)
            self.assertEqual(saved, before)
            for field in ('price', 'quantity'):
                self.assertEqual(Decimal(actual[field]), Decimal(previous[field]), (manual, field))
            for field in ('model', 'name', 'specs'):
                self.assertEqual(actual[field], previous[field], (manual, field))
            expected_origin = {**previous['catalog'], 'available': False,
                               'availability': current['products']['1']['availability']}
            self.assertEqual(actual['catalog'], expected_origin)
            self.assertTrue(any(actual['name'] in w and 'под заказ' in w.casefold()
                                for w in refreshed['variants']['economy']['warnings']))
            loaded = evaluate(json.loads(json.dumps(refreshed)))
            self.assertEqual(film(loaded)['catalog'], expected_origin)
            self.assertTrue(any('под заказ' in w.casefold() for w in loaded['variants']['economy']['warnings']))
            restored, _ = catalog.auto_select(loaded, initial_catalog, only_variant='economy')
            self.assertTrue(film(restored)['catalog']['available'])
            self.assertFalse(any('под заказ' in w.casefold() for w in restored['variants']['economy']['warnings']))
