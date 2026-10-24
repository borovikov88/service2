"""Synthetic public-card contracts; no live shop availability or prices."""
from copy import deepcopy
from decimal import Decimal
import json

from django.test import SimpleTestCase

from pool_service.services import estimate_catalog as catalog
from pool_service.services.estimate_candidates import assess
from pool_service.services.pool_estimates import generate, evaluate, recalculate
from .test_estimate_catalog import feed
from .test_estimate_catalog_params import item
from .test_pool_estimates import INPUTS, block


PARAMS = [('Тип товара', 'Насос'), ('Назначение', 'Фильтрация; Циркуляция'),
          ('Напряжение', '220'), ('Префильтр', 'Да'), ('Расход воды, м³/ч', '8'),
          ('Напор при указанной производительности, м', '8'),
          ('Режим указанной производительности', '8 м³/ч при 8 м; 220 В'),
          ('Число фаз', '1'), ('Частота питания, Гц', '50')]


def pump(pid=1, params=PARAMS, name='Насос Test', price='100', category=True):
    xml = item(pid, name, params=params, price=price)
    if not category:
        xml = xml.replace('<param name="Категория подбора">Насосы для бассейнов</param>', '')
    return xml


def row(snapshot, tier='economy'):
    return next(r for r in block(snapshot, 'equipment', tier)['rows'] if r['kind'] == 'pump')


class PumpFactsTests(SimpleTestCase):
    def test_public_card_defines_point_supply_and_saved_source_without_title_numbers(self):
        products = catalog.parse_catalog(feed(pump()))
        p = products['products']['1']
        self.assertEqual((p['nominal_flow'], p['flow'], p['head'], p['power']), ('8', '8', '8', '230'))
        self.assertTrue(p['pump_facts']['prefilter'])
        self.assertFalse(p['pump_facts']['issue'])
        initial, _ = catalog.auto_select(generate({**INPUTS, 'power': '230', 'required_head': '8'}), products)
        loaded = evaluate(json.loads(json.dumps(initial)))
        for tier in loaded['variants']:
            self.assertEqual(row(loaded, tier)['catalog']['pump_facts'], p['pump_facts'])
            self.assertEqual(Decimal(row(loaded, tier)['specs']['flow']), Decimal('8'))
            self.assertIn('расход воды', row(loaded, tier)['catalog']['pump_facts']['source'])

    def test_no_text_backfill_when_a_public_card_field_is_missing(self):
        params = [(k, v) for k, v in PARAMS if k not in {'Напряжение', 'Расход воды, м³/ч', 'Режим указанной производительности'}]
        p = catalog.parse_catalog(feed(pump(params=params, name='Насос Test 220V 12m3/h*8m')))['products']['1']
        self.assertIsNone(p['nominal_flow'])
        self.assertIsNone(p['flow'])
        self.assertEqual(p['power'], 'unknown')
        issue, missing = assess(p, {**INPUTS, 'power': '230'}, Decimal('8'))
        self.assertIsNone(issue)
        self.assertIn('питание модели', missing)
        self.assertIn('производительность модели', missing)

    def test_wrong_voltage_capacity_head_and_nonfiltration_are_excluded(self):
        for field, value in [('Напряжение', '380'), ('Расход воды, м³/ч', '2'),
                             ('Напор при указанной производительности, м', '4'),
                             ('Назначение', 'Гидромассаж'), ('Тип товара', 'Запчасть')]:
            params = [(k, value if k == field else v) for k, v in PARAMS if k != 'Режим указанной производительности']
            p = catalog.parse_catalog(feed(pump(params=params)))['products']['1']
            self.assertTrue(assess(p, {**INPUTS, 'power': '230', 'required_head': '8'}, Decimal('8'))[0], field)

    def test_repeated_unknown_or_conflicting_fields_do_not_confirm_a_neighbour(self):
        for extra in [('Напряжение', '380'), ('Префильтр', 'Не определён'),
                      ('Расход воды, м³/ч', 'Qmax 30'), ('Назначение', 'Неизвестно'),
                      ('Тип товара', 'Насос с другой характеристикой'),
                      ('Режим указанной производительности', '8 м³/ч при 4 м; 220 В')]:
            p = catalog.parse_catalog(feed(pump(params=PARAMS + [extra])))['products']['1']
            self.assertTrue(p['pump_facts']['issue'], extra)
            self.assertTrue(assess(p, INPUTS, Decimal('8'))[0])
        p = catalog.parse_catalog(feed(pump(params=PARAMS + [('Напряжение', '230 В')])))['products']['1']
        self.assertFalse(p['pump_facts']['issue'])

    def test_category_membership_not_generic_pump_name_controls_auto_and_picker(self):
        c = catalog.parse_catalog(feed(pump(category=False)))
        result, _ = catalog.auto_select(generate(INPUTS), c)
        self.assertIsNone(row(result)['catalog'])
        self.assertEqual(catalog.search_catalog(c, '', 'pump', '', INPUTS, Decimal('8'))['total'], 0)
        self.assertIn('категории', row(result)['selection_note'])
        for prefix in ('<category id="1">Новинки</category>', '<category id="1">Бассейны</category>'):
            xml = feed(pump(category=False)).decode().replace('<offers>', '<categories>' + prefix + '<category id="275" parentId="1">Насосы для бассейнов</category></categories><offers>').replace('<price>', '<categoryId>275</categoryId><price>', 1)
            p = catalog.parse_catalog(xml.encode())['products']['1']
            self.assertFalse(p['pump_facts']['in_category'])
        # A secondary-category marker works regardless of the feed's main category.
        xml = pump().replace('<price>', '<categoryId>99</categoryId><price>', 1)
        c = catalog.parse_catalog(feed(xml))
        self.assertEqual(catalog.search_catalog(c, '', 'pump', '', INPUTS, Decimal('8'))['total'], 1)

    def test_prefilter_is_preferred_before_price_and_external_prefilter_is_explicit(self):
        no_filter = [(k, 'Нет' if k == 'Префильтр' else v) for k, v in PARAMS]
        c = catalog.parse_catalog(feed(pump(1, no_filter, price='1'), pump(2, price='1000')))
        result, _ = catalog.auto_select(generate(INPUTS), c)
        for tier in result['variants']:
            self.assertEqual(row(result, tier)['catalog']['id'], '2')
        c = catalog.parse_catalog(feed(pump(1, no_filter)))
        result, _ = catalog.auto_select(generate(INPUTS), c)
        self.assertIn('отдельный префильтр', row(result)['selection_note'])

    def test_refresh_replaces_only_auto_and_preserves_manual_rows_prices_and_history(self):
        c = catalog.parse_catalog(feed(pump(1), pump(2, [(k, '10' if k == 'Расход воды, м³/ч' else v)
                                                      for k, v in PARAMS if k != 'Режим указанной производительности'], price='200')))
        initial, _ = catalog.auto_select(generate(INPUTS), c)
        old = deepcopy(initial)
        c['products']['1']['pump_facts']['issue'] = 'Изменённая карточка противоречива.'
        result, _ = catalog.auto_select(initial, c)
        self.assertEqual(row(result)['catalog']['id'], '2')
        self.assertEqual(initial, old)
        row(initial)['catalog']['selection'] = 'manual'
        row(initial)['price'] = '777'
        result, _ = catalog.auto_select(initial, c)
        self.assertEqual(row(result)['catalog']['id'], '1')
        self.assertEqual(Decimal(row(result)['price']), Decimal('777'))
        self.assertIn('противоречива', row(result)['catalog_review_note'])
        resized = recalculate(result, {**INPUTS, 'length': '20'})
        self.assertEqual(Decimal(row(resized)['price']), Decimal('777'))

    def test_other_duty_or_maxima_do_not_become_a_filtration_point(self):
        p = catalog.parse_catalog(feed(pump(name='Насос гидромассажный Test')))['products']['1']
        self.assertTrue(p['pump_facts']['issue'])
        params = [(k, v) for k, v in PARAMS if k not in {'Напор при указанной производительности, м', 'Режим указанной производительности'}]
        p = catalog.parse_catalog(feed(pump(params=params, name='Насос Test 8 м³/ч Hmax 18 м')))['products']['1']
        self.assertIsNone(p['flow'])
        self.assertIsNone(p['head'])
        self.assertIn('рабочая Q–H точка насоса', assess(p, INPUTS, Decimal('8'))[1])

    def test_verified_catalogue_purpose_aliases_and_operating_mode_wording(self):
        for duty in ['Фильтрация и циркуляция воды в бассейне',
                     'Циркуляция воды в фильтровальной системе бассейна',
                     'Циркуляция воды в системе фильтрации бассейна',
                     'Циркуляция и фильтрация общественных бассейнов',
                     'Фильтрация и циркуляция воды в общественных бассейнах',
                     'Циркуляция воды и фильтрация бассейна',
                     'Циркуляция и фильтрация воды бассейна']:
            for mode in ['8 м³/ч при 8 м; 220 В; 50 Гц', 'Рабочая точка при 8 м',
                         'Каталожная рабочая точка при 8 м, 50 Гц',
                         'Рабочая каталожная точка поставщика', '8 м³/ч при напоре 8 м']:
                params = [(k, duty if k == 'Назначение' else mode if k == 'Режим указанной производительности'
                           else '50/60' if k == 'Частота питания, Гц' else v) for k, v in PARAMS]
                p = catalog.parse_catalog(feed(pump(params=params)))['products']['1']
                self.assertFalse(p['pump_facts']['issue'], (duty, mode))
                self.assertIsNone(assess(p, INPUTS, Decimal('8'))[0])
        for mode in ['8 м³/ч при 10 м; 220 В; 50 Гц', '8 м³/ч при 8 м; 380 В', 'Рабочая точка при 4 м']:
            params = [(k, mode if k == 'Режим указанной производительности' else v) for k, v in PARAMS]
            p = catalog.parse_catalog(feed(pump(params=params)))['products']['1']
            self.assertTrue(p['pump_facts']['issue'])

    def test_speed_conditions_are_provisional_and_maxima_are_not_operating_points(self):
        for mode, has_point in [('При 1450 об/мин', True), ('Qmax 8 м³/ч, Hmax 8 м', False), ('Неизвестный режим', True)]:
            params = [(k, mode if k == 'Режим указанной производительности' else v) for k, v in PARAMS]
            p = catalog.parse_catalog(feed(pump(params=params)))['products']['1']
            self.assertFalse(p['pump_facts']['issue'])
            self.assertEqual(p['flow'] is not None, has_point)
            self.assertTrue(p['pump_facts']['mode_note'])
            self.assertIn(p['pump_facts']['mode_note'], assess(p, INPUTS, Decimal('8'))[1])

    def test_pool_circulation_with_prefilter_is_provisional_and_filtration_has_priority(self):
        params = [(k, 'Циркуляция воды плавательного бассейна' if k == 'Назначение'
                   else 'Центробежный насос с префильтром' if k == 'Тип товара' else v) for k, v in PARAMS]
        p = catalog.parse_catalog(feed(pump(params=params)))['products']['1']
        issue, missing = assess(p, INPUTS, Decimal('8'))
        self.assertIsNone(issue)
        self.assertTrue(p['pump_facts']['purpose_note'])
        self.assertIn(p['pump_facts']['purpose_note'], missing)
        c = catalog.parse_catalog(feed(pump(1, params, price='1'), pump(2, price='1000')))
        result, _ = catalog.auto_select(generate(INPUTS), c)
        self.assertEqual(row(result)['catalog']['id'], '2')
        # Unknown general purpose cannot be promoted using a prefilter alone.
        params = [(k, 'Оборудование бассейна' if k == 'Назначение' else v) for k, v in PARAMS]
        p = catalog.parse_catalog(feed(pump(params=params)))['products']['1']
        self.assertTrue(assess(p, INPUTS, Decimal('8'))[0])
