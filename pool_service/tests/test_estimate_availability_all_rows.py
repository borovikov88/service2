"""Stock policy at every existing automatic catalogue entry point."""
from copy import deepcopy
from decimal import Decimal

from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog
from pool_service.services.pool_estimates import generate
from .test_pool_estimates import INPUTS
from .test_estimate_catalog import offer, feed, fixture as core
from .test_estimate_skimmer import stock as embedded
from .test_estimate_primary import PRIMARY, fixture as primary
from .test_estimate_lighting import fixture as lighting
from .test_estimate_liner import stock as liner
from .test_estimate_filter_media import fixture as media
from .test_estimate_auxiliary import fixture as auxiliary
from .test_estimate_temperature_control import HEAT, fixture as temperature
from .test_estimate_litokol_contact import PARAMS, fixture as finishing


def extra_materials():
    from pool_service.services.estimate_liner_corner import URL
    return catalog.parse_catalog(feed(
        offer(1, 'Геотекстиль под пленку бассейна', description=''),
        offer(2, 'Крепёжный уголок ПВХ Aquaviva 0050032', url=URL, description='')))


def level():
    return catalog.parse_catalog(feed(offer(1, 'Регулятор уровня Aquaviva RO-7').replace(
        '</offer>', '<vendorCode>16791</vendorCode></offer>')))


def selected_rows(snapshot):
    return [(tier, row) for tier, variant in snapshot['variants'].items()
            for block in variant['blocks'] if not block['excluded']
            for row in block['rows'] if not row['excluded'] and row.get('catalog')]


class AllRowsStockTests(SimpleTestCase):
    def test_saved_separate_level_inlet_also_blocks_backorder(self):
        from .test_estimate_level_control import generate as generate_legacy
        from .test_estimate_catalog_params import REGS, nozzle
        source = catalog.parse_catalog(feed(REGS[0], nozzle()))
        initial = generate_legacy({**INPUTS, 'finish': 'film'})
        control, _ = catalog.auto_select(initial, source)
        self.assertTrue(any(r['kind'] == 'level_inlet' for _, r in selected_rows(control)))
        for product in source['products'].values():
            if product['kind'] == 'inlet':
                product.update(available=False, availability={
                    'state': 'preorder', 'label': 'Под заказ', 'issue': '', 'orderable': True})
        result, _ = catalog.auto_select(initial, source)
        self.assertFalse(any(r['kind'] == 'level_inlet' for _, r in selected_rows(result)))

    def test_every_automatic_family_blocks_backorder_and_keeps_manual_catalogue_choice(self):
        cases = [
            ('core', {**INPUTS, 'heating': 'electric', 'heating_power': '6', 'power': '230'}, core,
             {'pump', 'filter', 'heater'}),
            ('embedded', INPUTS, embedded, {'skimmer', 'inlet', 'drain', 'vacuum'}),
            ('primary', PRIMARY, primary, {'circulation_pump', 'solenoid_valve'}),
            ('lighting', {**INPUTS, 'lighting': 'yes', 'light_count': '2'}, lighting,
             {'pool_light', 'light_niche', 'light_transformer', 'light_box'}),
            ('liner', {**INPUTS, 'finish': 'film'}, liner, {'liner'}),
            ('media', INPUTS, media, {'filter_media'}),
            ('auxiliary', {**INPUTS, 'power': '230', 'required_head': '8'}, auxiliary,
             {'treatment', 'control_panel'}),
            ('temperature', HEAT, temperature, {'thermostat'}),
            ('materials', {**INPUTS, 'finish': 'film'}, extra_materials,
             {'geotextile', 'liner_corner'}),
            ('plaster', PARAMS, finishing, {'plaster_component'}),
            ('level', INPUTS, level, {'level_regulator'}),
        ]
        for name, inputs, factory, kinds in cases:
            source = factory()
            initial = generate(inputs)
            control, _ = catalog.auto_select(initial, source)
            for kind in kinds:
                with self.subTest(family=name, kind=kind):
                    chosen = [(tier, r) for tier, r in selected_rows(control) if r['kind'] == kind]
                    self.assertTrue(chosen, 'Positive stock control must select this family')
                    blocked = deepcopy(source)
                    denied = set()
                    for product in blocked['products'].values():
                        if product['kind'] == kind:
                            denied.add(product['id'])
                            product.update(available=False, availability={
                                'state': 'preorder', 'label': 'Под заказ', 'issue': '', 'orderable': True})
                    automatic, _ = catalog.auto_select(initial, blocked)
                    self.assertFalse(denied & {r['catalog']['id'] for _, r in selected_rows(automatic)})
                    tier, original = chosen[0]
                    product = blocked['products'][original['catalog']['id']]
                    search = catalog.search_catalog(blocked, '', kind, '', control['inputs'], Decimal('8'), variant=tier)
                    found = next(p for p in search['products'] if p['id'] == product['id'])
                    self.assertIn('под заказ', found['warning'].casefold())
                    manual = deepcopy(original)
                    catalog.apply_product(manual, product, blocked, 'manual')
                    self.assertEqual(manual['catalog']['id'], product['id'])
                    self.assertEqual(manual['catalog']['selection'], 'manual')
                    self.assertFalse(manual['catalog']['available'])
