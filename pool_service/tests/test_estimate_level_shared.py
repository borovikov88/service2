from copy import deepcopy
from decimal import Decimal
import json

from django.test import SimpleTestCase
from pool_service.services import pool_estimates as estimates
from pool_service.services import estimate_level_shared as shared
from pool_service.services import estimate_catalog as catalog
from .test_pool_estimates import INPUTS, block
from .test_estimate_catalog import offer, feed


def inlet(snapshot, tier='economy'):
    return next(r for r in block(snapshot, 'embedded', tier)['rows'] if r['kind'] == 'inlet')


class SharedLevelInletTests(SimpleTestCase):
    def test_one_row_total_and_duty_roundtrip_recalculation(self):
        s = estimates.generate(INPUTS)
        row = inlet(s)
        self.assertEqual(row['quantity'], '4.000')
        self.assertEqual(row['filtration_quantity'], '3.000')
        self.assertEqual(row['level_quantity'], '1.000')
        self.assertEqual(row['level_quantity_note'], 'Фильтрация: 3 шт.; линия уровня: 1 шт.')
        self.assertFalse(any(r['kind'] == 'level_inlet' for r in block(s, 'embedded')['rows']))
        row.update(price='125.25', unit_price_manual=True)
        s = estimates.evaluate(s)
        self.assertEqual(inlet(s)['amount'], '501.00')
        baseline = s['variants']['economy']['total']
        for _ in range(3):
            s = estimates.recalculate(json.loads(json.dumps(s)), s['inputs'])
            self.assertEqual(inlet(s)['quantity'], '4.000')
            self.assertEqual(shared.filtration_quantity(inlet(s)), Decimal(3))
            self.assertEqual(s['variants']['economy']['total'], baseline)
        self.assertIn('Проверить', ' '.join(s['variants']['economy']['warnings']))

    def test_builtin_toggle_and_exclusion_do_not_charge_extra(self):
        s = estimates.generate(INPUTS)
        inlet(s)['price'] = '100'
        s = estimates.evaluate(s)
        baseline = Decimal(s['variants']['economy']['total'])
        skimmer = next(r for r in block(s, 'embedded')['rows'] if r['kind'] == 'skimmer')
        skimmer.update(level_status='included', level_evidence='Паспорт конкретной модели')
        s = estimates.evaluate(s)
        self.assertEqual(inlet(s)['level_quantity'], '0.000')
        self.assertEqual(Decimal(s['variants']['economy']['total']), baseline - 100)
        skimmer = next(r for r in block(s, 'embedded')['rows'] if r['kind'] == 'skimmer')
        skimmer.update(level_status='absent')
        s = estimates.evaluate(s)
        self.assertEqual(Decimal(s['variants']['economy']['total']), baseline)
        reg = next(r for r in block(s, 'embedded')['rows'] if r['kind'] == 'level_regulator')
        reg.update(excluded=True, reason='Исключено пользователем')
        s = estimates.evaluate(s)
        self.assertEqual(inlet(s)['level_quantity'], '0.000')
        self.assertEqual(Decimal(s['variants']['economy']['total']), baseline - 100)

    def test_manual_quantity_price_zero_and_split_validation(self):
        s = estimates.generate(INPUTS)
        inlet(s).update(auto_quantity=False, quantity='7', price='123.45', unit_price_manual=True)
        s = estimates.evaluate(s)
        self.assertEqual(inlet(s)['quantity'], '7.000')
        self.assertEqual(inlet(s)['filtration_quantity'], '6.000')
        self.assertEqual(inlet(s)['amount'], '864.15')
        s = estimates.recalculate(s, {**s['inputs'], 'length': '16'})
        self.assertEqual(inlet(s)['amount'], '864.15')
        inlet(s)['quantity'] = '0'
        s = estimates.evaluate(s)
        self.assertEqual(inlet(s)['level_quantity'], '0.000')
        self.assertEqual(inlet(s)['amount'], '0.00')
        inlet(s)['level_quantity'] = '2'
        with self.assertRaises(estimates.EstimateInputError):
            estimates.evaluate(s)

    def test_two_models_need_binding_and_binding_is_variant_local(self):
        s = estimates.generate(INPUTS)
        other = deepcopy(inlet(s)); other['id'] = 'other_inlet'; other['name'] = 'Другая модель'
        block(s, 'embedded')['rows'].append(other)
        s = estimates.evaluate(s)
        self.assertTrue(all(r['level_quantity'] == '0.000' for r in block(s, 'embedded')['rows'] if r['kind'] == 'inlet'))
        self.assertIn('нескольких моделях', ' '.join(s['variants']['economy']['warnings']))
        s = shared.apply_binding(s, 'economy', 'other_inlet')
        self.assertEqual(inlet(s)['level_quantity'], '0.000')
        other = next(r for r in block(s, 'embedded')['rows'] if r['id'] == 'other_inlet')
        self.assertEqual(other['level_quantity'], '1.000')
        self.assertFalse(s['variants']['standard'].get('level_inlet_link_manual', False))
        s = shared.apply_binding(s, 'economy', '')
        self.assertTrue(all(r['level_quantity'] == '0.000' for r in block(s, 'embedded')['rows'] if r['kind'] == 'inlet'))

    def test_known_incompatible_retains_manual_model_without_auto_extra(self):
        s = estimates.generate(INPUTS)
        c = catalog.parse_catalog(feed(offer(21, 'Форсунка Aquaviva ABS под пленку', price='100')))
        p = c['products']['21']; p.update(level_passage='no')
        catalog.apply_product(inlet(s), p, c, 'manual')
        s = estimates.evaluate(s)
        row = inlet(s)
        self.assertEqual(row['catalog']['id'], '21')
        self.assertEqual(row['price'], '100.00')
        self.assertEqual(row['level_quantity'], '0.000')
        self.assertIn('свободного прохода', ' '.join(s['variants']['economy']['warnings']))

    def test_legacy_pair_is_not_migrated_on_recalculate(self):
        s = estimates.generate(INPUTS)
        s.pop('level_shared_inlet')
        for v in s['variants'].values():
            for b in v['blocks']:
                for r in b['rows']:
                    r.pop('filtration_quantity', None); r.pop('level_quantity', None)
        s = estimates.recalculate(s, s['inputs'])
        self.assertNotIn('level_shared_inlet', s)
        self.assertEqual(len([r for r in block(s, 'embedded')['rows'] if r['kind'] == 'level_inlet']), 1)
        self.assertEqual(inlet(s)['quantity'], '3.000')

    def test_reserved_inlet_does_not_supply_filtration_and_repair_preserves_split(self):
        from pool_service.services.estimate_compatibility import repair
        s = estimates.generate(INPUTS)
        pump = next(r for r in block(s)['rows'] if r['kind'] == 'pump')
        pump['specs'].update(flow='9', head='8')
        inlet(s).update(quantity='3', auto_quantity=False, price='100')
        s = estimates.evaluate(s)
        self.assertEqual(inlet(s)['filtration_quantity'], '2.000')
        self.assertIn('нужно от 3', inlet(s)['compatibility']['message'])
        fixed = repair(s, 'economy', 'embedded', inlet(s)['id'])
        self.assertEqual(inlet(fixed)['quantity'], '4.000')
        self.assertEqual(inlet(fixed)['amount'], '400.00')
        self.assertIsNone(inlet(fixed)['compatibility'])
        self.assertEqual(inlet(fixed)['level_quantity'], '1.000')

    def test_regulator_selection_keeps_same_fitting_model_and_manual_price(self):
        c = catalog.parse_catalog(feed(
            offer(21, 'Форсунка Aquaviva ABS под пленку', price='100'),
            offer(22, 'Регулятор уровня Aquaviva RO-7', price='4000').replace('</offer>', '<vendorCode>16791</vendorCode></offer>')))
        s = estimates.generate(INPUTS)
        catalog.apply_product(inlet(s), c['products']['21'], c, 'manual')
        inlet(s).update(price='123.45', unit_price_manual=True)
        from pool_service.services.estimate_level_control import fill
        fill(s, c)
        s = estimates.evaluate(s)
        reg = next(r for r in block(s, 'embedded')['rows'] if r['kind'] == 'level_regulator')
        self.assertEqual(reg['catalog']['id'], '22')
        self.assertEqual(inlet(s)['catalog']['id'], '21')
        self.assertEqual(inlet(s)['amount'], '493.80')
        self.assertIn('Проверить совместимость', ' '.join(s['variants']['economy']['warnings']))
        before = s['variants']['economy']['total']
        fill(s, c)
        s = estimates.evaluate(s)
        self.assertEqual(s['variants']['economy']['total'], before)
        self.assertFalse(any(r['kind'] == 'level_inlet' for r in block(s, 'embedded')['rows']))

    def test_manual_quantity_before_binding_and_existing_separate_row_preserved(self):
        s = estimates.generate(INPUTS)
        row = inlet(s)
        row.update(auto_quantity=False, quantity='5', filtration_quantity='5', level_quantity='0', price='100')
        s = shared.apply_binding(s, 'economy', row['id'])
        self.assertEqual(inlet(s)['quantity'], '5.000')
        self.assertEqual(inlet(s)['level_quantity'], '0.000')
        self.assertIn('ручное количество', ' '.join(s['variants']['economy']['warnings']))
        legacy = estimates.make_row('old_level', 'Сохранённая форсунка', 'шт.', '1', '', 'level_inlet')
        legacy.update(auto_quantity=False, price='200')
        block(s, 'embedded')['rows'].append(legacy)
        inlet(s)['auto_quantity'] = True
        s = estimates.evaluate(s)
        self.assertEqual(inlet(s)['level_quantity'], '0.000')
        self.assertEqual(next(r for r in block(s, 'embedded')['rows'] if r['id'] == 'old_level')['amount'], '200.00')
        self.assertIn('во избежание дублирования', ' '.join(s['variants']['economy']['warnings']))

    def test_known_connection_conflict_and_excluded_fitting_are_not_replaced(self):
        c = catalog.parse_catalog(feed(
            offer(21, 'Форсунка Aquaviva ABS под пленку', price='100'),
            offer(22, 'Регулятор уровня Aquaviva RO-7', price='4000').replace('</offer>', '<vendorCode>16791</vendorCode></offer>')))
        for fact, inlet_value, regulator_value in [('level_diameter', '50 mm', '40 mm'), ('level_connection', 'glue_socket', 'bsp_female')]:
            with self.subTest(fact=fact):
                s = estimates.generate(INPUTS)
                c['products']['21'][fact] = inlet_value
                c['products']['22'][fact] = regulator_value
                catalog.apply_product(inlet(s), c['products']['21'], c, 'manual')
                reg = next(r for r in block(s, 'embedded')['rows'] if r['kind'] == 'level_regulator')
                catalog.apply_product(reg, c['products']['22'], c, 'manual')
                s = estimates.evaluate(s)
                self.assertEqual(inlet(s)['level_quantity'], '0.000')
                self.assertEqual(inlet(s)['catalog']['id'], '21')
                self.assertIn('несовпадение соединений', ' '.join(s['variants']['economy']['warnings']))
                c['products']['21'].pop(fact); c['products']['22'].pop(fact)
        s = estimates.generate(INPUTS)
        inlet(s).update(excluded=True, reason='Исключено пользователем', price='100')
        s = estimates.evaluate(s)
        self.assertTrue(inlet(s)['excluded'])
        self.assertEqual(inlet(s)['level_quantity'], '0.000')
        with self.assertRaises(estimates.EstimateInputError):
            shared.apply_binding(s, 'economy', inlet(s)['id'])

    def test_existing_regulator_is_physical_but_not_repriced_or_duplicated(self):
        s = estimates.generate(INPUTS)
        reg = next(r for r in block(s, 'embedded')['rows'] if r['kind'] == 'level_regulator')
        reg.update(existing=True, excluded=True, price='4000', auto_quantity=False)
        inlet(s)['price'] = '100'
        s = estimates.evaluate(s)
        self.assertEqual(inlet(s)['level_quantity'], '1.000')
        reg = next(r for r in block(s, 'embedded')['rows'] if r['kind'] == 'level_regulator')
        self.assertTrue(reg['existing'])
        self.assertTrue(reg['excluded'])
        self.assertEqual(reg['price'], '4000.00')
        self.assertIsNone(reg['amount'])
        self.assertEqual(inlet(s)['amount'], '400.00')

    def test_live_regulator_conflict_withdrawal_replacement_and_same_id_fact_refresh(self):
        def stock(price='4000', conflict=False, replacement=False):
            rows = [offer(22, 'Регулятор уровня Aquaviva RO-7', price=price).replace('</offer>', '<vendorCode>16791</vendorCode></offer>')]
            if replacement:
                rows.append(offer(23, 'Регулятор уровня Kripsol RGP.C', brand='Kripsol', price='11000').replace('</offer>', '<vendorCode>528</vendorCode></offer>'))
            result = catalog.parse_catalog(feed(*rows))
            if conflict:
                result['products']['22'].update(level_data_issue='conflict', specification_note='Диаметр 50 мм / 40 мм противоречив.')
            return result
        def reg(s):
            return next(r for r in block(s, 'embedded')['rows'] if r['kind'] == 'level_regulator')
        initial, _ = catalog.auto_select(estimates.generate(INPUTS), stock())
        self.assertEqual(reg(initial)['amount'], '4000.00')
        before = Decimal(initial['variants']['economy']['total'])
        updated = stock(price='5500')
        updated['products']['22']['level_diameter'] = '50 mm'
        refreshed, _ = catalog.auto_select(initial, updated)
        self.assertEqual(reg(refreshed)['catalog']['level_diameter'], '50 mm')
        self.assertEqual(reg(refreshed)['catalog']['price'], '4000.00')
        self.assertEqual(reg(refreshed)['price'], '4000.00')
        self.assertEqual(Decimal(refreshed['variants']['economy']['total']), before)
        removed, _ = catalog.auto_select(refreshed, stock(conflict=True))
        self.assertIsNone(reg(removed)['catalog'])
        self.assertIsNone(reg(removed)['amount'])
        self.assertIn('отклонён', reg(removed)['selection_note'])
        self.assertEqual(Decimal(removed['variants']['economy']['total']), before - 4000)
        restored = estimates.recalculate(json.loads(json.dumps(removed)), removed['inputs'])
        self.assertIsNone(reg(restored)['amount'])
        self.assertEqual(restored['variants']['economy']['total'], removed['variants']['economy']['total'])
        replacement, _ = catalog.auto_select(initial, stock(conflict=True, replacement=True))
        self.assertEqual(reg(replacement)['catalog']['id'], '23')
        self.assertEqual(reg(replacement)['amount'], '11000.00')
        self.assertEqual(Decimal(replacement['variants']['economy']['total']), before + 7000)

    def test_live_regulator_conflict_preserves_protected_price_quantity_and_warning(self):
        c = catalog.parse_catalog(feed(offer(22, 'Регулятор уровня Aquaviva RO-7', price='4000').replace('</offer>', '<vendorCode>16791</vendorCode></offer>')))
        initial, _ = catalog.auto_select(estimates.generate(INPUTS), c)
        bad = deepcopy(c)
        bad['products']['22'].update(level_data_issue='conflict', specification_note='Диаметр 50 мм / 40 мм противоречив.')
        for protection in ('manual_price', 'manual_selection', 'manual_quantity'):
            with self.subTest(protection=protection):
                s = deepcopy(initial)
                reg = next(r for r in block(s, 'embedded')['rows'] if r['kind'] == 'level_regulator')
                if protection == 'manual_price':
                    reg.update(price='4321.23', unit_price_manual=True)
                elif protection == 'manual_selection':
                    reg['catalog']['selection'] = 'manual'
                else:
                    reg.update(quantity='2', auto_quantity=False)
                expected_price, expected_qty = Decimal(reg['price']), Decimal(reg['quantity'])
                result, _ = catalog.auto_select(s, bad)
                result = estimates.recalculate(json.loads(json.dumps(result)), result['inputs'])
                kept = next(r for r in block(result, 'embedded')['rows'] if r['kind'] == 'level_regulator')
                self.assertEqual(Decimal(kept['price']), expected_price)
                self.assertEqual(Decimal(kept['quantity']), expected_qty)
                self.assertEqual(Decimal(kept['amount']), expected_price * expected_qty)
                self.assertEqual(kept['catalog']['id'], '22')
                self.assertNotEqual(kept['catalog']['level_data_issue'], 'conflict')
                self.assertIn('Текущая выгрузка', kept['catalog_review_note'])
                self.assertIn('Противоречивые характеристики', ' '.join(result['variants']['economy']['warnings']))
