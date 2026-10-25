"""Synthetic card facts and lifecycle checks; not live stock or prices."""
from copy import deepcopy
from decimal import Decimal
import json

from django.test import SimpleTestCase, TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from unittest.mock import patch
from pool_service.models import Organization, OrganizationAccess
from pool_service.services import estimate_catalog as catalog
from pool_service.services.estimate_candidates import assess
from pool_service.services.estimate_compatibility import repair
from pool_service.services.pool_estimates import generate, evaluate, recalculate, EstimateInputError
from .test_estimate_catalog import offer, feed
from .test_estimate_catalog_params import item
from .test_pool_estimates import INPUTS, block

PARAMS = [('Тип фильтра', 'Песочный'), ('Расход воды, м³/ч', '8'),
          ('Диаметр фильтра, мм', '455'), ('Площадь фильтрации, м²', '0.16'),
          ('Шестипозиционный клапан в комплекте', 'Да'), ('Расположение клапана', 'Боковое'),
          ('Подключение', '1,5 дюйма / 50 мм'), ('Модель производителя', 'S450'),
          ('Загрузка песка, кг', '45')]


def filtr(pid=1, params=PARAMS, price='100', sku='F1', name='Фильтр Test', category=True):
    xml = item(pid, name, sku, price=price, params=params)
    return xml if category else xml.replace('<param name="Категория подбора">Фильтры</param>', '')


def without_valve():
    return [(k, 'Нет' if k == 'Шестипозиционный клапан в комплекте' else v) for k, v in PARAMS]


def valve(pid=3, compatible='F1', price='50', sku='V1', name='Клапан Hayward SIDE 6-ти поз. 1,5" (SP0719SE)'):
    params = [('Категория подбора', 'Запчасти для фильтров и фильтрационных установок'),
              ('Расположение клапана', 'Боковое'), ('Подключение', '1,5 дюйма / 50 мм')]
    if compatible is not None:
        params.append(('Совместимые фильтры (артикулы)', compatible))
    return item(pid, name, sku, price=price, params=params)


def filter_row(snapshot, tier='economy'):
    return next(r for r in block(snapshot, 'equipment', tier)['rows'] if r['kind'] == 'filter')


def valve_rows(snapshot, tier='economy'):
    return [r for b in snapshot['variants'][tier]['blocks'] for r in b['rows'] if r['kind'] == 'filter_valve']


def selected(params=None, extra=()):
    c = catalog.parse_catalog(feed(filtr(params=without_valve() if params is None else params), *extra))
    s, _ = catalog.auto_select(generate(INPUTS), c)
    return s, c


class FilterFactsTests(SimpleTestCase):
    def test_card_flow_area_diameter_completeness_and_loading_are_saved(self):
        c = catalog.parse_catalog(feed(filtr(name='Фильтр Test 99 м3/ч')))
        p = c['products']['1']
        self.assertEqual(p['filter_flow'], '8')
        self.assertEqual(p['filter_facts']['diameter_mm'], '455')
        self.assertEqual(p['filter_facts']['velocity_m_h'], '50')
        self.assertTrue(p['filter_facts']['valve_included'])
        self.assertEqual(p['filter_loading']['quartz_kg'], '45')
        s, _ = catalog.auto_select(generate(INPUTS), c)
        loaded = evaluate(json.loads(json.dumps(s)))
        self.assertEqual(filter_row(loaded)['catalog']['filter_facts'], p['filter_facts'])
        self.assertFalse(valve_rows(loaded))

    def test_missing_card_capacity_is_not_filled_from_title_or_outer_diameter(self):
        params = [(k, v) for k, v in PARAMS if k in {'Тип фильтра', 'Диаметр фильтра, мм'}]
        p = catalog.parse_catalog(feed(filtr(params=params, name='Фильтр Test 99 м3/ч')))['products']['1']
        self.assertIsNone(p['filter_flow'])
        self.assertIsNone(p['filter_facts']['velocity_m_h'])
        self.assertIn('производительность модели', assess(p, INPUTS, Decimal('8'))[1])
        legacy = catalog.parse_catalog(feed(filtr(params=[])))['products']['1']
        self.assertIn('характеристики фильтра', ' '.join(assess(legacy, INPUTS, Decimal('8'))[1]))

    def test_strict_category_and_repeated_conflicting_capacity(self):
        for xml in [filtr(category=False), filtr(params=PARAMS + [('Расход воды, м³/ч', '12')])]:
            c = catalog.parse_catalog(feed(xml))
            s, _ = catalog.auto_select(generate(INPUTS), c)
            self.assertIsNone(filter_row(s)['catalog'])
        c = catalog.parse_catalog(feed(filtr(category=False)))
        self.assertEqual(catalog.search_catalog(c, '', 'filter', '', INPUTS, Decimal('8'))['total'], 0)
        params = PARAMS + [('Шестипозиционный клапан в комплекте', 'Нет')]
        s, _ = selected(params)
        self.assertIsNone(filter_row(s)['catalog']['filter_facts']['valve_included'])
        self.assertFalse(valve_rows(s))
        self.assertTrue(any('комплект' in w for w in s['variants']['economy']['warnings']))

    def test_filter_is_sized_to_selected_pump_using_card_capacity(self):
        small = filtr(name='Фильтр Small 99 м3/ч')
        large = filtr(2, [(k, '12' if k == 'Расход воды, м³/ч' else v) for k, v in PARAMS], price='200', sku='F2')
        pump = offer(10, 'Насос Test 12m3/h*8m 220V')
        c = catalog.parse_catalog(feed(small, large, pump))
        s, _ = catalog.auto_select(generate({**INPUTS, 'power': '230', 'required_head': '8'}), c)
        self.assertEqual(filter_row(s)['catalog']['id'], '2')

    def test_no_valve_adds_a_separate_unpriced_position_and_tracks_filter_quantity(self):
        s, _ = selected()
        for tier in s['variants']:
            row = valve_rows(s, tier)[0]
            self.assertEqual(row['filter_parent'], filter_row(s, tier)['id'])
            self.assertEqual(Decimal(row['quantity']), Decimal('1'))
            self.assertIsNone(row['price'])
            self.assertIn('совместимость', row['selection_note'])
        filter_row(s)['quantity'] = '2'
        s = evaluate(s)
        self.assertEqual(Decimal(valve_rows(s)[0]['quantity']), Decimal('2'))
        filter_row(s)['unit'] = 'кг'
        s = evaluate(s)
        self.assertIsNone(valve_rows(s)[0]['quantity'])
        self.assertIn('целое количество', valve_rows(s)[0]['selection_note'])

    def test_new_public_valve_alias_accepts_yes_no_but_position_count_is_not_evidence(self):
        for value in ('Нет', 'Да', None):
            with self.subTest(valve_included=value):
                params = [(k, v) for k, v in PARAMS if k != 'Шестипозиционный клапан в комплекте']
                params.append(('Число позиций вентиля', '6'))
                if value is not None:
                    params.append(('Вентиль в комплекте', value))
                s, c = selected(params)
                self.assertEqual(c['products']['1']['filter_facts']['valve_included'], {'Нет': False, 'Да': True, None: None}[value])
                self.assertEqual(len(valve_rows(s)), 1 if value == 'Нет' else 0)
                if value == 'Нет':
                    r = valve_rows(s)[0]
                    self.assertIsNone(r.get('catalog'))
                    self.assertIsNone(r['price'])
                    filter_row(s)['quantity'] = '2'
                    self.assertEqual(valve_rows(evaluate(s))[0]['quantity'], '2.000')

    def test_conflicting_old_and_new_completeness_labels_remain_unknown(self):
        for old, new in (('Нет', 'Да'), ('Да', 'Нет'), ('Нет', 'Нет')):
            with self.subTest(old=old, new=new):
                params = [(k, v) for k, v in PARAMS if k != 'Шестипозиционный клапан в комплекте']
                params.extend([('Шестипозиционный клапан в комплекте', old), ('Вентиль в комплекте', new)])
                s, c = selected(params)
                facts = c['products']['1']['filter_facts']
                if old != new:
                    self.assertIsNone(facts['valve_included'])
                    self.assertIn('вентиль в комплекте', facts['valve_note'])
                    self.assertFalse(valve_rows(s))
                else:
                    self.assertIs(facts['valve_included'], False)
                    self.assertEqual(facts['valve_note'], '')
                    self.assertEqual(len(valve_rows(s)), 1)

    def test_independent_hydraulic_issue_does_not_hide_explicit_valve_completeness(self):
        for value in ('Нет', 'Да', None):
            with self.subTest(valve_included=value):
                params = [(k, v) for k, v in PARAMS if k != 'Шестипозиционный клапан в комплекте']
                params.append(('Расход воды, м³/ч', '12'))  # Conflicts with the existing 8.
                if value is not None:
                    params.append(('Шестипозиционный клапан в комплекте', value))
                c = catalog.parse_catalog(feed(filtr(params=params), valve()))
                s = generate(INPUTS)
                catalog.apply_product(filter_row(s), c['products']['1'], c)
                filter_row(s)['quantity'] = '2'
                updated = evaluate(s)
                self.assertIn('расход воды', filter_row(updated)['catalog']['filter_facts']['issue'])
                self.assertIn('расход воды', assess(c['products']['1'], INPUTS, Decimal('8'))[0])
                if value == 'Нет':
                    self.assertEqual(len(valve_rows(updated)), 1)
                    valve_row = valve_rows(updated)[0]
                    self.assertEqual(valve_row['filter_parent'], filter_row(updated)['id'])
                    self.assertEqual(valve_row['quantity'], '2.000')
                    self.assertIsNone(valve_row.get('catalog'))
                    self.assertIsNone(valve_row['price'])
                    self.assertIsNone(valve_row['amount'])
                    self.assertIn('совместимость', valve_row['selection_note'])
                else:
                    self.assertFalse(valve_rows(updated))
                    unknown_warning = any('входит ли' in w for w in updated['variants']['economy']['warnings'])
                    self.assertEqual(unknown_warning, value is None)
                refreshed, _ = catalog.auto_select(updated, c, only_variant='economy')
                self.assertEqual(len(valve_rows(refreshed)), 1 if value == 'Нет' else 0)
                self.assertTrue(all(r.get('catalog') is None and r['price'] is None for r in valve_rows(refreshed)))

    def test_old_completeness_interpretation_stays_frozen_until_explicit_card_refresh(self):
        c = catalog.parse_catalog(feed(filtr(params=without_valve() + [('Расход воды, м³/ч', '12')])))
        old = generate(INPUTS)
        for tier in old['variants']:
            catalog.apply_product(filter_row(old, tier), c['products']['1'], c)
            filter_row(old, tier)['catalog']['filter_facts'].pop('valve_independent')
        old = evaluate(old)
        before = deepcopy(old)
        self.assertEqual(evaluate(old), before)
        recalculated = recalculate(old, old['inputs'])
        for tier in old['variants']:
            self.assertFalse(valve_rows(recalculated, tier))
            self.assertEqual(filter_row(recalculated, tier)['catalog'], filter_row(before, tier)['catalog'])
            self.assertEqual(recalculated['variants'][tier]['total'], before['variants'][tier]['total'])
        catalog.apply_product(filter_row(old), c['products']['1'], c)
        accepted = evaluate(old)
        self.assertEqual(len(valve_rows(accepted)), 1)
        self.assertIsNone(valve_rows(accepted)[0].get('catalog'))
        for tier in ('standard', 'premium'):
            self.assertEqual(accepted['variants'][tier], before['variants'][tier])

    def test_only_explicit_compatibility_can_select_a_valve_and_price_stays_frozen(self):
        s, c = selected(extra=(valve(),))
        self.assertEqual(valve_rows(s)[0]['catalog']['id'], '3')
        original = deepcopy(s)
        c['products']['3']['price'] = '999'
        updated, _ = catalog.auto_select(s, c)
        self.assertEqual(valve_rows(updated)[0]['price'], '50.00')
        self.assertEqual(s, original)
        for compatibility in (None, 'OTHER'):
            s, _ = selected(extra=(valve(compatible=compatibility),))
            self.assertIsNone(valve_rows(s)[0]['catalog'])
        c = catalog.parse_catalog(feed(valve(), valve(4, name='Ротор для 6-ти позиционного клапана')))
        self.assertEqual(c['products']['4']['kind'], 'other')
        self.assertEqual(catalog.search_catalog(c, '', 'filter_valve', '', INPUTS, Decimal('8'))['total'], 1)
        wrong_type = valve().replace('</offer>', '<param name="Тип товара">Ротор клапана</param></offer>')
        p = catalog.parse_catalog(feed(wrong_type))['products']['3']
        self.assertEqual(p['kind'], 'other')
        for connection in ('2 дюйма / 63 мм', 'DN50', '1 1/2 дюйма'):
            xml = valve().replace('1,5 дюйма / 50 мм</param>', connection + '</param>')
            s, _ = selected(extra=(xml,))
            self.assertIsNone(valve_rows(s)[0]['catalog'], connection)

    def test_included_and_unknown_remove_only_untouched_generated_valves(self):
        s, c = selected(extra=(valve(),))
        catalog.apply_product(filter_row(s), c['products']['1'], c)
        included_catalog = catalog.parse_catalog(feed(filtr(), valve()))
        updated, _ = catalog.auto_select(s, included_catalog)
        self.assertFalse(valve_rows(updated))
        protected = deepcopy(s)
        row = valve_rows(protected)[0]
        row['catalog']['selection'] = 'manual'
        row.update(price='777', quantity='3', auto_quantity=False)
        updated, _ = catalog.auto_select(protected, included_catalog)
        self.assertEqual(Decimal(valve_rows(updated)[0]['price']), Decimal('777'))
        self.assertEqual(Decimal(valve_rows(updated)[0]['quantity']), Decimal('3'))
        self.assertIn('дублирование', valve_rows(updated)[0]['selection_note'])
        unknown = [(k, v) for k, v in PARAMS if k != 'Шестипозиционный клапан в комплекте']
        unknown_snapshot, _ = selected(unknown)
        self.assertFalse(valve_rows(unknown_snapshot))
        self.assertTrue(any('входит ли' in w for w in unknown_snapshot['variants']['economy']['warnings']))

    def test_user_deletion_exclusions_and_history_are_preserved_on_recalculation(self):
        s, c = selected(extra=(valve(),))
        before = deepcopy(s)
        saved = evaluate(json.loads(json.dumps(s)))
        self.assertEqual(valve_rows(saved)[0]['catalog'], valve_rows(s)[0]['catalog'])
        row = valve_rows(s)[0]
        row.update(excluded=True, reason='Решение пользователя', selection_locked=True, quantity='7')
        updated, _ = catalog.auto_select(recalculate(s, s['inputs']), c)
        actual = valve_rows(updated)[0]
        self.assertTrue(actual['excluded'])
        self.assertEqual(actual['reason'], 'Решение пользователя')
        self.assertEqual(Decimal(actual['quantity']), Decimal('7'))
        s = deepcopy(before)
        for b in s['variants']['economy']['blocks']:
            b['rows'] = [r for r in b['rows'] if r['kind'] != 'filter_valve']
        updated, _ = catalog.auto_select(recalculate(s, s['inputs']), c)
        self.assertFalse(valve_rows(updated))
        self.assertTrue(any('строка отсутствует' in w for w in updated['variants']['economy']['warnings']))
        self.assertEqual(valve_rows(before)[0]['catalog']['id'], '3')

    def test_filter_replacement_deduplicates_only_generated_rows_and_checks_new_model(self):
        s, c = selected(extra=(valve(),))
        duplicate = deepcopy(valve_rows(s)[0])
        duplicate['id'] = 'duplicate'
        block(s, 'equipment')['rows'].append(duplicate)
        self.assertEqual(len(valve_rows(evaluate(s))), 1)
        changed = catalog.parse_catalog(feed(filtr(2, without_valve(), sku='F2'), valve()))
        catalog.apply_product(filter_row(s), changed['products']['2'], changed)
        updated, _ = catalog.auto_select(s, changed)
        self.assertEqual(filter_row(updated)['catalog']['id'], '2')
        self.assertEqual(len(valve_rows(updated)), 1)
        self.assertIsNone(valve_rows(updated)[0]['catalog'])
        filter_row(updated)['excluded'] = True
        self.assertTrue(valve_rows(evaluate(updated))[0]['excluded'])
        filter_row(updated)['excluded'] = False
        self.assertFalse(valve_rows(evaluate(updated))[0]['excluded'])

    def test_point_filter_repair_uses_card_facts_and_syncs_the_external_valve(self):
        params = [(k, '12' if k == 'Расход воды, м³/ч' else v) for k, v in without_valve()]
        c = catalog.parse_catalog(feed(filtr(), filtr(2, params, sku='F2'),
            offer(10, 'Насос Test 12m3/h*8m 220V')))
        s, _ = catalog.auto_select(generate({**INPUTS, 'power': '230', 'required_head': '8'}), c)
        catalog.apply_product(filter_row(s), c['products']['1'], c)
        s = evaluate(s)
        self.assertTrue(filter_row(s)['compatibility']['can_auto'])
        updated = repair(s, 'economy', 'equipment', filter_row(s)['id'], c)
        self.assertEqual(filter_row(updated)['catalog']['id'], '2')
        self.assertTrue(valve_rows(updated))

    def test_legacy_snapshots_gain_no_valve_rows_until_explicit_upgrade(self):
        s, c = selected()
        s.pop('filter_valve_version')
        for variant in s['variants'].values():
            variant.pop('filter_valve_parents', None)
            variant.pop('filter_valve_active', None)
            for b in variant['blocks']:
                b['rows'] = [r for r in b['rows'] if r['kind'] != 'filter_valve']
        self.assertFalse(valve_rows(evaluate(s)))
        upgraded, _ = catalog.auto_select(s, c)
        self.assertTrue(valve_rows(upgraded))

    def test_existing_filter_does_not_quote_a_new_valve_and_manual_choice_is_preserved(self):
        s, c = selected(extra=(valve(),))
        before = deepcopy(s)
        filter_row(s).update(existing=True, quantity='3')
        updated, _ = catalog.auto_select(s, c)
        self.assertFalse(valve_rows(updated))
        self.assertEqual(Decimal(filter_row(updated)['quantity']), Decimal('3'))
        self.assertEqual(filter_row(updated)['price'], filter_row(s)['price'])
        protected = deepcopy(before)
        filter_row(protected).update(existing=True, quantity='3')
        row = valve_rows(protected)[0]
        row['catalog']['selection'] = 'manual'
        row.update(price='777', quantity='7', auto_quantity=False)
        updated, _ = catalog.auto_select(protected, c)
        self.assertEqual(Decimal(valve_rows(updated)[0]['quantity']), Decimal('7'))
        self.assertEqual(Decimal(valve_rows(updated)[0]['price']), Decimal('777'))
        self.assertIn('существующий', valve_rows(updated)[0]['selection_note'])

    def test_price_only_refresh_warns_about_new_completeness_without_adopting_facts(self):
        s, _ = selected(extra=(valve(),))
        current = catalog.parse_catalog(feed(filtr(), valve()))
        before = deepcopy(filter_row(s)['catalog']['filter_facts'])
        updated, _, _ = catalog.refresh_prices(s, current, 'economy')
        self.assertEqual(filter_row(updated)['catalog']['filter_facts'], before)
        self.assertTrue(valve_rows(updated))
        self.assertIn('Комплектность', filter_row(updated)['catalog_review_note'])
        accepted, _ = catalog.auto_select(updated, current, only_variant='economy')
        self.assertFalse(valve_rows(accepted))

    def test_malformed_and_orphan_links_do_not_create_unrelated_purchases(self):
        s, _ = selected(extra=(valve(),))
        row = valve_rows(s)[0]
        row['filter_parent'] = 'missing-filter'
        row['catalog']['selection'] = 'manual'
        row.update(price='777', quantity='7', auto_quantity=False)
        updated = evaluate(s)
        self.assertEqual(len(valve_rows(updated)), 1)
        self.assertEqual(Decimal(valve_rows(updated)[0]['price']), Decimal('777'))
        self.assertIn('Не определён связанный фильтр', valve_rows(updated)[0]['selection_note'])
        bad = deepcopy(updated)
        bad['variants']['economy']['filter_valve_parents'] = {'bad/link': '1:no'}
        with self.assertRaises(EstimateInputError):
            evaluate(bad)

    def test_point_repair_of_legacy_snapshot_activates_only_the_selected_quote(self):
        s, c = selected()
        params = [(k, '12' if k == 'Расход воды, м³/ч' else v) for k, v in without_valve()]
        current = catalog.parse_catalog(feed(filtr(), filtr(2, params, sku='F2'),
            offer(10, 'Насос Test 12m3/h*8m 220V')))
        for variant in s['variants'].values():
            variant.pop('filter_valve_active', None)
            variant.pop('filter_valve_parents', None)
            for b in variant['blocks']:
                b['rows'] = [r for r in b['rows'] if r['kind'] != 'filter_valve']
        s.pop('filter_valve_version')
        pump = next(r for r in block(s, 'equipment')['rows'] if r['kind'] == 'pump')
        catalog.apply_product(pump, current['products']['10'], current)
        s['inputs']['required_head'] = '8'
        before = evaluate(s)
        updated = repair(before, 'economy', 'equipment', filter_row(before)['id'], current)
        self.assertTrue(valve_rows(updated))
        for tier in ('standard', 'premium'):
            self.assertEqual(updated['variants'][tier], before['variants'][tier])


class FilterCardApiTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name='Synthetic filter tenant')
        self.user = User.objects.create_user('filter-card-owner')
        OrganizationAccess.objects.create(user=self.user, organization=self.org, role='owner')
        self.client.force_login(self.user)
        self.url = reverse('pool_estimate_api')

    def post(self, **data):
        return self.client.post(self.url, json.dumps({'organization': self.org.pk, **data}), content_type='application/json')

    @patch('pool_service.estimate_views.get_catalog')
    def test_explicit_filter_apply_upgrades_only_one_quote_and_keeps_history_unchanged(self, get_catalog):
        params = [(('Вентиль в комплекте' if k == 'Шестипозиционный клапан в комплекте' else k), v) for k, v in without_valve()]
        c = catalog.parse_catalog(feed(filtr(params=params)))
        get_catalog.return_value = c
        old = generate(INPUTS)
        filter_row(old)['quantity'] = '2'
        old.pop('filter_valve_version')
        for variant in old['variants'].values():
            variant.pop('filter_valve_active', None)
            variant.pop('filter_valve_parents', None)
        saved = self.post(action='save', snapshot=old, expected_revision=0).json()
        before = evaluate(old)
        response = self.post(action='catalog_apply', snapshot=old, variant='economy',
                             block='equipment', row='filter', product_id='1')
        self.assertEqual(response.status_code, 200)
        chosen = response.json()['snapshot']
        self.assertTrue(valve_rows(chosen))
        self.assertEqual(valve_rows(chosen)[0]['quantity'], '2.000')
        self.assertIsNone(valve_rows(chosen)[0].get('catalog'))
        self.assertIsNone(valve_rows(chosen)[0]['price'])
        for tier in ('standard', 'premium'):
            self.assertEqual(chosen['variants'][tier], before['variants'][tier])
        get_catalog.reset_mock()
        historical = self.client.get(self.url, {'organization': self.org.pk, 'id': saved['id'], 'version': 1}).json()['snapshot']
        self.assertFalse(valve_rows(historical))
        get_catalog.assert_not_called()
