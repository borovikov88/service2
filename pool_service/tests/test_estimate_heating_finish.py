"""Synthetic regressions for heater sizing, reversible finishes and frozen dates."""
from copy import deepcopy
from decimal import Decimal

from django.test import SimpleTestCase

from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import EstimateInputError, evaluate, generate, recalculate
from pool_service.tests.test_pool_estimates import INPUTS, block
from pool_service.tests.test_estimate_catalog import feed, fixture, offer

HEATING = {**INPUTS, 'heating': 'electric', 'heater_brand': 'any', 'power': '230',
           'water_start': '15', 'water_target': '16', 'heating_hours': '24', 'heating_reserve': '20'}


def heater(snapshot, variant='economy'):
    return next(r for r in block(snapshot, variant=variant)['rows'] if r['kind'] == 'heater')


class HeatingFinishTests(SimpleTestCase):
    def test_heating_uses_volume_temperature_time_reserve_and_manual_override(self):
        calculated = generate({**HEATING, 'water_target': '25', 'heating_reserve': '25'})
        # 48 m3 * 10 C * 1.16 kWh/(m3*C) / 24 h * 1.25 = 29 kW.
        self.assertEqual(calculated['heating'], {'mode': 'estimated', 'power': '29.000'})
        self.assertEqual(generate({**HEATING, 'heating_power': '6'})['heating'], {'mode': 'manual', 'power': '6.000'})
        self.assertEqual(generate({**INPUTS, 'heating': 'electric'})['heating']['mode'], 'incomplete')
        for changes in ({'water_target': '14'}, {'heating_hours': '0'}, {'heating_reserve': '-1'}, {'water_start': 'NaN'}):
            with self.subTest(changes=changes), self.assertRaises(EstimateInputError):
                generate({**HEATING, **changes})
        self.assertTrue(any('теплопотери' in w.lower() for w in calculated['variants']['economy']['warnings']))

    def test_heaters_follow_price_tiers_independently_of_old_brand_selector(self):
        f = catalog.parse_catalog(feed(
            offer(81, 'Электронагреватель Aquaviva Test 3 (220V, 3 кВт)', price='100'),
            offer(82, 'Электронагреватель Other Test 3 (220V, 3 кВт)', brand='Other', price='200'),
            offer(83, 'Электронагреватель Elecro Test Titan 3 (220V, 3 кВт)', brand='Elecro', price='300'),
            offer(84, 'Электронагреватель Elecro Giant Titan (220V, 30 кВт)', brand='Elecro', price='900'),
            offer(85, 'Электронагреватель Aquaviva Wrong supply (380V, 3 кВт)', price='1'),
            offer(86, 'Электронагреватель Aquaviva Unavailable (220V, 3 кВт)', price='1', available='false'),
            offer(87, 'Электронагреватель Aquaviva Weak (220V, 2 кВт)', price='1'),
        ))
        for old_choice in ('any', 'variant', 'Hayward'):
            s, count = catalog.auto_select(generate({**HEATING, 'heater_brand': old_choice}), f)
            self.assertEqual(count, 3)
            self.assertEqual([heater(s, v)['catalog']['id'] for v in s['variants']], ['81', '82', '83'])
            self.assertEqual([s['variants'][v]['total'] for v in s['variants']], ['100.00', '200.00', '300.00'])
        changed, _ = catalog.auto_select(recalculate(s, {**HEATING, 'heating_hours': '6'}), f)
        self.assertEqual(heater(changed)['catalog']['id'], '84')
        self.assertEqual(heater(s)['catalog']['id'], '81')
        for edit in ({'price': '77.77'}, {'selection_locked': True}, {'excluded': True}, {'existing': True}, {'quantity': '2'}):
            manual = deepcopy(s); heater(manual).update(edit)
            result, _ = catalog.auto_select(recalculate(manual, {**HEATING, 'heating_hours': '6'}), f)
            self.assertEqual(heater(result)['catalog']['id'], '81')
            if 'price' in edit:
                self.assertEqual(heater(result)['price'], '77.77')
                self.assertTrue(any('мощность ниже' in w for w in result['variants']['economy']['warnings']))

    def test_dual_supply_titles_are_eligible_and_persist_without_false_voltage_warning(self):
        for suffix in ('220В/380В', '220/380 В', '230 V / 400 V'):
            f = catalog.parse_catalog(feed(offer(91, f'Электронагреватель Elecro Test Titan 9кВт {suffix}', brand='Elecro')))
            self.assertEqual(f['products']['91']['power_options'], ['230', '400'])
            for supply in ('230', '400'):
                s, count = catalog.auto_select(generate({**HEATING, 'heating_power': '9', 'power': supply}), f)
                self.assertEqual(count, 3)
                self.assertEqual(heater(s)['catalog']['power_options'], ['230', '400'])
                self.assertFalse(any('питание не соответствует' in w or 'напряжение питания' in w for w in s['variants']['economy']['warnings']))
        unknown = catalog.parse_catalog(feed(offer(92, 'Электронагреватель Test 9кВт', description='Модели 220В и 380В; мощность 9 или 12 кВт')))
        s, _ = catalog.auto_select(generate({**HEATING, 'heating_power': '9'}), unknown)
        self.assertIsNone(heater(s)['price'])

    def test_brand_and_titanium_preferences_cannot_invert_price_tiers(self):
        for prices in (('1000', '100', '200'), ('200', '300', '100')):
            f = catalog.parse_catalog(feed(
                offer(91, 'Электронагреватель Aquaviva Test 3кВт 220В', price=prices[0]),
                offer(92, 'Электронагреватель Other Test 3кВт 220В', brand='Other', price=prices[1]),
                offer(93, 'Электронагреватель Elecro Titan Test 3кВт 220В', brand='Elecro', price=prices[2]),
                offer(94, 'Электронагреватель Aquaviva Test Titan 30кВт 220В', price='1'),
            ))
            s, _ = catalog.auto_select(generate(HEATING), f)
            selected = [heater(s, v) for v in s['variants']]
            amounts = [Decimal(r['price']) for r in selected]
            self.assertEqual(amounts, sorted(amounts))
            self.assertTrue(all(r['catalog']['heating_power'] == '3.000' for r in selected))

    def test_missing_aquaviva_uses_real_alternative_with_explanation_and_manual_choice_stays(self):
        f = catalog.parse_catalog(feed(
            offer(91, 'Нагреватель для бассейнов 220В, 3 кВт', brand='Other', price='100'),
            offer(92, 'Электронагреватель Elecro Test Titan 3кВт 220В', brand='Elecro', price='200'),
            offer(93, 'Электронагреватель Elecro Test Titan 3кВт 220В', brand='Elecro', price='300'),
            offer(94, 'Электрический нагреватель воздуха для осушителя Test 3кВт 220В', price='1'),
        ))
        s, _ = catalog.auto_select(generate(HEATING), f)
        self.assertEqual([heater(s, v)['catalog']['id'] for v in s['variants']], ['91', '92', '93'])
        self.assertTrue(any('альтернатива Aquaviva' in w for w in s['variants']['economy']['warnings']))
        catalog.apply_product(heater(s), f['products']['93'], f)
        result, _ = catalog.auto_select(s, f)
        self.assertEqual(heater(result)['catalog']['id'], '93')
        self.assertEqual(heater(result)['catalog']['selection'], 'manual')
        # Old snapshots without the new optional metadata remain readable.
        heater(s)['catalog'].pop('power_options'); heater(s)['catalog'].pop('titanium')
        self.assertEqual(heater(evaluate(s))['catalog']['power_options'], ['230'])

    def test_heating_off_is_reversible_and_manual_exclusions_stay(self):
        s, _ = catalog.auto_select(generate({**HEATING, 'heating_power': '6'}), fixture())
        heater(s, 'standard').update(excluded=True, reason='По просьбе клиента')
        off = recalculate(s, {**HEATING, 'heating': 'none'})
        self.assertTrue(heater(off)['excluded'])
        self.assertIsNone(heater(off)['amount'])
        restored = recalculate(off, {**HEATING, 'heating_power': '6'})
        self.assertFalse(heater(restored)['excluded'])
        self.assertEqual(heater(restored)['price'], '300.00')
        self.assertTrue(heater(restored, 'standard')['excluded'])
        self.assertEqual(heater(restored, 'standard')['reason'], 'По просьбе клиента')

    def test_finish_switch_retains_rates_and_restores_only_system_exclusions(self):
        s = generate({**INPUTS, 'finish': 'film'})
        original = block(s, 'finish')['rows'][0]
        original['price'] = '1200'
        block(s, 'finish')['rows'][1].update(excluded=True, reason='Работы заказчика')
        tile = recalculate(s, {**INPUTS, 'finish': 'tile'})
        old = block(tile, 'finish')['rows'][0]
        self.assertEqual(old['price'], '1200.00')
        self.assertTrue(old['excluded'])
        for v in tile['variants']:
            active = [r for r in block(tile, 'finish', v)['rows'] if not r['excluded']]
            self.assertEqual(len(active), 2)
            self.assertTrue(all(r['finish_type'] == 'tile' and r['price'] is None and r['quantity'] == '68.000' for r in active))
        film = recalculate(tile, {**INPUTS, 'finish': 'film'})
        self.assertFalse(block(film, 'finish')['rows'][0]['excluded'])
        self.assertTrue(block(film, 'finish')['rows'][1]['excluded'])
        self.assertEqual(len(block(film, 'finish')['rows']), 5)
        self.assertEqual(film['variants']['economy']['total'], '94800.00')
        self.assertFalse(original['excluded'])

    def test_finish_changes_preserve_block_scope_and_legacy_rows(self):
        old = generate(INPUTS)
        # Simulate an actual snapshot predating the finish selector.
        old['inputs'].pop('finish')
        for v in old['variants'].values():
            for b in v['blocks']:
                for r in b['rows']:
                    r.pop('finish_type', None)
        block(old, 'finish')['excluded'] = True
        block(old, 'finish')['rows'][0]['price'] = '900'
        updated = recalculate(old, {**INPUTS, 'finish': 'mosaic', 'length': '10'})
        self.assertTrue(block(updated, 'finish')['excluded'])
        self.assertEqual(block(updated, 'finish')['rows'][0]['price'], '900.00')
        self.assertEqual(len(block(updated, 'finish')['rows']), 4)
        self.assertTrue(all(r['quantity'] == '82.000' for r in block(updated, 'finish')['rows']))
        self.assertEqual(evaluate(old)['inputs']['finish'], '')
        for value in ('wood', [], None):
            with self.assertRaises(EstimateInputError):
                generate({**INPUTS, 'finish': value})

    def test_unselected_finish_rates_do_not_remain_active_after_selecting_tile(self):
        s = recalculate(generate({**INPUTS, 'finish': 'film'}), {**INPUTS, 'finish': ''})
        for row in block(s, 'finish')['rows']:
            if not row['excluded']:
                row['price'] = '100'
        tile = recalculate(s, {**INPUTS, 'finish': 'tile'})
        active = [r for r in block(tile, 'finish')['rows'] if not r['excluded']]
        self.assertEqual(len(active), 2)
        self.assertTrue(all(r['finish_type'] == 'tile' for r in active))
        self.assertEqual(tile['variants']['economy']['total'], '0.00')
        restored = recalculate(tile, {**INPUTS, 'finish': ''})
        self.assertEqual(restored['variants']['economy']['total'], '13600.00')

    def test_one_price_date_does_not_claim_all_versions_have_newest_prices(self):
        old, _ = catalog.auto_select(generate(INPUTS), fixture())
        newer = fixture(); newer['checked_at'] = '2026-10-22T00:00:00+00:00'
        fresh, _, _ = catalog.refresh_prices(old, newer, 'economy')
        self.assertEqual(fresh['prices']['checked_at'], '2026-10-19T00:00:00+00:00')
        self.assertTrue(fresh['prices']['mixed'])
        block(fresh, variant='standard')['excluded'] = True
        block(fresh, variant='premium')['excluded'] = True
        new = evaluate(fresh)
        self.assertEqual(new['prices']['checked_at'], newer['checked_at'])
        block(new)['rows'][0]['price'] = '77.77'
        self.assertTrue(evaluate(new)['prices']['manual'])
        tampered = deepcopy(old); tampered['prices'] = {'checked_at': '2099-01-01'}
        self.assertEqual(evaluate(tampered)['prices'], old['prices'])
        search = catalog.search_catalog(newer, '', 'heater', '', generate(HEATING)['inputs'], Decimal('8'))
        self.assertEqual(search['status']['checked_at'], newer['checked_at'])
        self.assertEqual(old['prices']['checked_at'], '2026-10-19T00:00:00+00:00')

    def test_finish_search_matches_both_spellings(self):
        f = catalog.parse_catalog(feed(offer(91, 'Пленка для бассейна Test', description=''), offer(92, 'Плёнка ПВХ Test', description='')))
        found = catalog.search_catalog(f, 'плёнка', 'other', '', generate(INPUTS)['inputs'], Decimal('8'))
        self.assertEqual(found['total'], 2)
