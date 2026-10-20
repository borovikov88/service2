"""Synthetic contracts: sources, exact bowl geometry, frozen legacy totals and scope."""
from copy import deepcopy
from decimal import Decimal
from django.test import SimpleTestCase, TestCase
from django.contrib.auth.models import User
from django.urls import reverse
from django.template.loader import render_to_string
from pool_service.models import Organization, OrganizationAccess
from pool_service.estimate_models import PoolEstimate
from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import generate, evaluate, recalculate, EstimateInputError
from pool_service.tests.test_pool_estimates import INPUTS, block
from pool_service.tests.test_estimate_catalog import feed, offer

BOWL = {**INPUTS, 'length': '7.77', 'width': '2.48', 'depth_start': '1.58', 'depth_end': '1.58',
        'bowl_height': '1.70', 'wall_thickness': '.20', 'slab_thickness': '.20', 'finish': 'film'}


def active_heaters(s, variant='economy'):
    return [r for r in block(s, variant=variant)['rows'] if r['kind'] == 'heater' and not r['excluded']]


class MultiGeometryTests(SimpleTestCase):
    def test_full_power_per_source_and_no_duplicate_shared_labor(self):
        inputs = {**INPUTS, 'heating_types': ['electric', 'exchanger', 'heatpump'], 'heating_power': '6', 'power': '230'}
        products = catalog.parse_catalog(feed(
            offer(81, 'Электронагреватель Test 6 кВт 220В', price='100'),
            offer(82, 'Теплообменник Test 6 кВт', price='200'),
            offer(83, 'Тепловой насос Test 6 кВт 220В', price='300'),
            offer(84, 'Электронагреватель Test 2 кВт 220В', price='1'),
        ))
        baseline = generate(inputs)
        selected, _ = catalog.auto_select(baseline, products)
        for tier in selected['variants']:
            self.assertEqual(len(active_heaters(selected, tier)), 3)
            self.assertEqual({r['catalog']['id'] for r in active_heaters(selected, tier)}, {'81', '82', '83'})
            self.assertEqual(Decimal(selected['variants'][tier]['total']) - Decimal(baseline['variants'][tier]['total']), 600)
            rows = block(selected, variant=tier)['rows']
            for name in ('piping', 'equipment_work', 'circulation_pump', 'solenoid_valve'):
                self.assertEqual(sum(r['id'] == name for r in rows), 1)
            self.assertEqual(sum(r['kind'] == 'pump' for r in rows), 1)
        again, _ = catalog.auto_select(recalculate(selected, inputs), products)
        self.assertEqual([v['total'] for v in selected['variants'].values()], [v['total'] for v in again['variants'].values()])

    def test_legacy_row_infers_type_and_manual_exclusion_survives(self):
        s = generate({**INPUTS, 'heating': 'electric', 'heating_power': '6'})
        for v in s['variants']:
            active_heaters(s, v)[0].pop('heating_type')
        active_heaters(s)[0].update(price='123', model='ручной выбор')
        before = evaluate(s)
        expanded = recalculate(s, {**INPUTS, 'heating_types': ['electric', 'exchanger'], 'heating_power': '6'})
        self.assertEqual(len(active_heaters(expanded)), 2)
        self.assertEqual(active_heaters(expanded)[0]['price'], '123.00')
        self.assertEqual(before['variants']['economy']['total'], expanded['variants']['economy']['total'])
        active_heaters(expanded)[0].update(excluded=True, reason='Клиент')
        off = recalculate(expanded, {**INPUTS, 'heating_types': []})
        on = recalculate(off, {**INPUTS, 'heating_types': ['electric', 'exchanger']})
        self.assertEqual([r['heating_type'] for r in active_heaters(on)], ['exchanger'])
        self.assertEqual(len([r for r in block(on)['rows'] if r['kind'] == 'circulation_pump']), 1)
        # Validation does not invent missing accessories or reprice history.
        legacy = deepcopy(before)
        legacy['inputs']['heating'] = 'exchanger'
        self.assertFalse(any(r['kind'] == 'circulation_pump' for r in block(evaluate(legacy))['rows']))

    def test_two_choices_standard_and_premium_use_expensive_and_sole_option_all(self):
        for names in [('81',), ('81', '82')]:
            c = catalog.parse_catalog(feed(*(offer(n, 'Электронагреватель Test 6 кВт 220В', price=str(i * 100 + 100), brand='Other') for i, n in enumerate(names))))
            s, _ = catalog.auto_select(generate({**INPUTS, 'heating': 'electric', 'heating_power': '6'}), c)
            self.assertEqual([active_heaters(s, v)[0]['catalog']['id'] for v in s['variants']], [names[0], names[-1], names[-1]])
        c = catalog.parse_catalog(feed(offer(90, 'Насос Other 8м3/ч 220В', brand='Other')))
        s, _ = catalog.auto_select(generate(INPUTS), c)
        self.assertEqual([block(s, variant=v)['rows'][0]['catalog']['id'] for v in s['variants']], ['90'] * 3)

    def test_accessories_never_masquerade_as_filtration_or_confirmed_selection(self):
        c = catalog.parse_catalog(feed(offer(91, 'Циркуляционный насос Test 220В', price='3000'), offer(92, 'Электромагнитный клапан Test 220В')))
        s, _ = catalog.auto_select(generate({**INPUTS, 'heating': 'exchanger'}), c)
        for kind in ('circulation_pump', 'solenoid_valve'):
            row = next(r for r in block(s)['rows'] if r['kind'] == kind)
            self.assertIsNone(row['price'])
        self.assertTrue(any('Первичный контур' in w for w in s['variants']['economy']['warnings']))

    def test_review_regressions_zero_manual_type_and_scoped_autoselect(self):
        s = generate(INPUTS)
        block(s, 'construction')['rows'][0].update(quantity='0', auto_quantity=False)
        changed = recalculate(s, BOWL)
        self.assertEqual(block(changed, 'construction')['rows'][0]['quantity'], '0.000')
        c = catalog.parse_catalog(feed(offer(91, 'Теплообменник Test 6 кВт')))
        s = generate({**INPUTS, 'heating':'electric'})
        catalog.apply_product(active_heaters(s)[0], c['products']['91'], c)
        changed = recalculate(s, {**INPUTS, 'heating_types':['electric', 'exchanger']})
        self.assertEqual({r['heating_type'] for r in active_heaters(changed)}, {'electric', 'exchanger'})
        legacy = generate({**INPUTS, 'heating':'exchanger'})
        for variant in legacy['variants'].values():
            for b in variant['blocks']:
                b['rows'] = [r for r in b['rows'] if r['kind'] not in ('circulation_pump', 'solenoid_valve')]
        legacy = evaluate(legacy)
        standard = deepcopy(legacy['variants']['standard'])
        scoped, _ = catalog.auto_select(legacy, c, only_variant='economy')
        self.assertEqual(scoped['variants']['standard'], standard)
        manual = generate(INPUTS)
        work = next(r for r in block(manual, 'construction')['rows'] if r['work_code'] == 'bowl_concrete')
        work.update(quantity=None, unit='компл.', price='100000', selection_locked=True)
        kept = recalculate(manual, BOWL)
        work = next(r for r in block(kept, 'construction')['rows'] if r['work_code'] == 'bowl_concrete')
        self.assertEqual(work['unit'], 'компл.')
        self.assertIsNone(work['quantity'])
        self.assertIsNone(work['amount'])
        incomplete = recalculate(generate(BOWL), {**BOWL, 'slab_thickness':''})
        concrete = next(r for r in block(incomplete, 'construction')['rows'] if r['id'] == 'bowl_material')
        self.assertIsNone(concrete['quantity'])
        self.assertIsNone(concrete['amount'])

    def test_solenoid_synonyms_are_catalogued_with_unconfirmed_compatibility(self):
        c = catalog.parse_catalog(feed(offer(91, 'Клапан соленоидный Aquaviva 2W31 DN25-G1', price='100')))
        self.assertEqual(c['products']['91']['kind'], 'solenoid_valve')
        inputs = generate({**INPUTS, 'heating':'exchanger'})['inputs']
        for query in ('электромагнитный', 'соленоидный', 'DN25'):
            result = catalog.search_catalog(c, query, 'solenoid_valve', '', inputs, Decimal('8'))
            self.assertEqual(result['total'], 1)
            self.assertIn('подтверждение', result['products'][0]['warning'])
        snapshot = generate({**INPUTS, 'heating':'exchanger'})
        row = next(r for r in block(snapshot)['rows'] if r['kind'] == 'solenoid_valve')
        catalog.apply_product(row, c['products']['91'], c)
        checked = evaluate(snapshot)
        self.assertEqual(next(r for r in block(checked)['rows'] if r['kind'] == 'solenoid_valve')['price'], '100.00')
        self.assertTrue(any('Первичный контур' in w for w in checked['variants']['economy']['warnings']))

    def test_circulation_section_classifies_generic_name_without_inventing_duty(self):
        c = catalog.parse_catalog(feed(
            offer(91, 'Насос Aquaviva Test Qmax 2.8м3/ч Hmax 4м', price='100',
                  url='https://shop.aqualine22.ru/tsirkulyatsionnye-nasosy/test.html'),
            offer(92, 'Циркуляционный насос Test с частотным регулированием', price='200'),
            offer(93, 'Циркуляционный насос Test без частотного регулирования', price='150'),
        ))
        simple = c['products']['91']
        self.assertEqual(simple['kind'], 'circulation_pump')
        for field in ('flow', 'head', 'nominal_flow', 'frequency_control'):
            self.assertIsNone(simple[field])
        self.assertIs(c['products']['92']['frequency_control'], True)
        self.assertIs(c['products']['93']['frequency_control'], False)
        inputs = generate({**INPUTS, 'heating':'exchanger'})['inputs']
        for tier, expected in (('economy','91'), ('standard','92'), ('premium','92')):
            result = catalog.search_catalog(c, '', 'circulation_pump', '', inputs, Decimal('8'), variant=tier)
            self.assertEqual(result['products'][0]['id'], expected)
            self.assertTrue(all(p['warning'] for p in result['products']))
        snapshot, _ = catalog.auto_select(generate({**INPUTS, 'heating':'exchanger'}), c)
        self.assertIsNone(next(r for r in block(snapshot)['rows'] if r['kind']=='circulation_pump')['price'])

    def test_category_identifies_circulation_without_reclassifying_accessories(self):
        offers = []
        for pid, name in ((91, 'Насос Aquaviva Test Qmax 2.8м3/ч Hmax 4м'),
                          (92, 'Муфта для циркуляционного насоса'),
                          (93, 'Тепловой насос Test 6 кВт 220В')):
            offers.append(offer(pid, name).replace('</offer>', '<categoryId>42</categoryId></offer>'))
        xml = feed(*offers).replace(b'<shop>', '<shop><categories><category id="42">Циркуляционные насосы</category></categories>'.encode())
        c = catalog.parse_catalog(xml)
        self.assertEqual(c['products']['91']['kind'], 'circulation_pump')
        self.assertFalse(c['products']['91']['filtration'])
        self.assertIsNone(c['products']['91']['nominal_flow'])
        self.assertIsNone(c['products']['91']['head'])
        self.assertEqual(c['products']['92']['kind'], 'other')
        self.assertEqual(c['products']['93']['heater_type'], 'heatpump')
        snapshot, _ = catalog.auto_select(generate(INPUTS), c)
        for variant in snapshot['variants']:
            self.assertIsNone(block(snapshot, variant=variant)['rows'][0]['catalog'])

    def test_print_uses_saved_dimensions_and_does_not_invent_legacy_height(self):
        snapshot = generate(BOWL)
        before = deepcopy(snapshot)
        def printable(value):
            return render_to_string('pool_service/estimates/print.html', {'version':{'snapshot':value, 'number':1}})
        html = printable(snapshot)
        for label, value in (('Полная внутренняя высота чаши H', '1.700'), ('Толщина стен', '0.200'), ('Толщина плиты', '0.200')):
            self.assertIn(f'{label}, м: <strong>{value}</strong>', html)
        self.assertIn('глубина воды 1.580–1.580', html)
        self.assertEqual(snapshot, before)
        legacy = printable(generate(INPUTS))
        self.assertEqual(legacy.count('<strong>не указана</strong>'), 3)
        partial = printable(generate({**BOWL, 'wall_thickness': '', 'slab_thickness': ''}))
        self.assertEqual(partial.count('<strong>не указана</strong>'), 2)
        self.assertIn('H, м: <strong>1.700</strong>', partial)

    def test_exact_geometry_controls_and_water_level_independence(self):
        s = generate(BOWL)
        g = s['geometry']
        for key, expected in {'volume':'30.445968', 'slab_concrete':'4.70592', 'wall_concrete':'7.242', 'concrete_total':'11.94792', 'finish_area':'54.1196', 'finish_net':'55', 'finish_stock':'63'}.items():
            self.assertEqual(Decimal(g[key]), Decimal(expected), key)
        second = {**BOWL, 'length':'6', 'width':'3', 'bowl_height':'1.5', 'depth_start':'1.4', 'depth_end':'1.4'}
        s2 = generate(second)
        for key, expected in {'volume':'25.2','slab_concrete':'4.352','wall_concrete':'5.64','concrete_total':'9.992','finish_area':'45','finish_stock':'52'}.items():
            self.assertEqual(Decimal(s2['geometry'][key]), Decimal(expected), key)
        for v in s['variants']:
            concrete = next(r for r in block(s, 'construction', v)['rows'] if r['work_code'] == 'bowl_concrete')
            self.assertEqual(Decimal(concrete['quantity']), 12)
            self.assertEqual(Decimal(concrete['amount']), 24000)
            work = next(r for r in block(s, 'finish', v)['rows'] if r['id'] == 'finish_work')
            self.assertEqual(Decimal(work['quantity']), 55)
        low = generate({**second, 'depth_start':'1', 'depth_end':'1'})
        for key in ('finish_area', 'concrete_total', 'finish_stock'):
            self.assertEqual(low['geometry'][key], s2['geometry'][key])

    def test_geometry_validation_manual_zero_and_staleness(self):
        for changes in ({'bowl_height':'1'}, {'wall_thickness':'0'}, {'slab_thickness':'-1'}, {'depth_end':'1.6'}):
            with self.subTest(changes=changes), self.assertRaises(EstimateInputError):
                generate({**BOWL, **changes})
        s = generate(BOWL)
        row = next(r for r in block(s, 'construction')['rows'] if r['work_code'] == 'bowl_concrete')
        row.update(quantity='0', auto_quantity=False)
        changed = recalculate(s, {**BOWL, 'length':'8'})
        row = next(r for r in block(changed, 'construction')['rows'] if r['work_code'] == 'bowl_concrete')
        self.assertEqual(row['quantity'], '0.000')
        self.assertTrue(row['quantity_stale'])
        self.assertEqual(row['amount'], '0.00')
        self.assertNotEqual(block(changed, 'construction')['rows'], block(changed, 'construction', 'standard')['rows'])
        for bad in ('electric', ['electric', 'electric'], ['unknown'], [None]):
            with self.assertRaises(EstimateInputError):
                generate({**INPUTS, 'heating_types':bad})


class OrganizationContextTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='context-owner')
        self.one = Organization.objects.create(name='Synthetic One')
        self.two = Organization.objects.create(name='Synthetic Two')
        self.foreign = Organization.objects.create(name='Synthetic Foreign')
        for org in (self.one, self.two):
            OrganizationAccess.objects.create(user=self.user, organization=org, role='owner')
        self.client.force_login(self.user)

    def test_hidden_context_validates_url_persists_session_and_resolves_saved_project(self):
        page = reverse('pool_estimate_page')
        response = self.client.get(page, {'organization':self.two.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'data-organization="{self.two.pk}"')
        self.assertNotContains(response, 'id="pe-org"')
        self.assertContains(self.client.get(page), f'data-organization="{self.two.pk}"')
        self.assertEqual(self.client.get(page, {'organization':self.foreign.pk}).status_code, 403)
        estimate = PoolEstimate.objects.create(organization=self.one, title='Synthetic', created_by=self.user)
        self.assertContains(self.client.get(page, {'id':estimate.pk}), f'data-organization="{self.one.pk}"')
