from copy import deepcopy
from decimal import Decimal as D

from django.test import SimpleTestCase

from pool_service.services import estimate_plaster_systems as plaster
from pool_service.services.pool_estimates import make_row

INPUTS = {'length': '8', 'width': '4', 'bowl_height': '1.65', 'wall_thickness': '.2',
          'slab_thickness': '.2', 'tech_plaster': 'walls_floor', 'tech_plaster_thickness': '.02'}


def snapshot():
    return {'plaster_systems': 1, 'inputs': dict(INPUTS), 'variants': {
        tier: {'blocks': [{'id': 'finish', 'rows': [make_row('tech_plaster_material',
               'Штукатурный слой: объём', 'м³', '2', 'tech_plaster_volume')]}]}
        for tier in ('economy', 'standard', 'premium')}}


def rows(s, tier='standard'):
    return s['variants'][tier]['blocks'][0]['rows']


class PlasterSystemTests(SimpleTestCase):
    def test_exact_geometry_stock_and_bags_without_film_allowance(self):
        for tier, latex in [('standard', '82.4832'), ('premium', '45.824')]:
            values = plaster.metrics(INPUTS, tier)
            self.assertEqual(values['area'], D('71.6'))
            self.assertEqual(values['mix_net_kg'], D('2291.2'))
            self.assertEqual(values['mix_stock_kg'], D('2405.76'))
            self.assertEqual(values['mix_purchase_bags'], D(97))
            self.assertEqual(values['latex_net'], D(latex))
        self.assertEqual(plaster.metrics(INPUTS, 'standard')['latex_quantity'], D('86.608'))
        self.assertEqual(plaster.metrics(INPUTS, 'premium')['latex_quantity'], D('48.116'))

    def test_fresh_rows_prices_unknown_and_repeat_does_not_duplicate(self):
        s = snapshot()
        warnings = plaster.apply(s, create=True)
        self.assertEqual(len(rows(s, 'economy')), 3)
        for tier in ('standard', 'premium'):
            self.assertEqual(len(rows(s, tier)), 3)
            self.assertEqual(rows(s, tier)[0]['quantity'], '97')
            self.assertIsNone(rows(s, tier)[2]['quantity'])
            self.assertTrue(all(r['price'] is None for r in rows(s, tier)))
            self.assertTrue(warnings[tier])
        saved = deepcopy(s)
        plaster.apply(s, create=True)
        self.assertEqual(s, saved)
        self.assertIn('плотность не подтверждена', rows(s, 'premium')[1]['construction_quantity_note'])

    def test_old_versions_are_unchanged(self):
        s = snapshot()
        del s['plaster_systems']
        saved = deepcopy(s)
        plaster.apply(s, create=True)
        self.assertEqual(s, saved)

    def test_manual_placeholder_never_gets_duplicate_system_or_unit_change(self):
        for change in ({'auto_quantity': False}, {'price': '900'}, {'unit': 'кг'},
                       {'existing': True}, {'excluded': True}, {'selection_locked': True}):
            s = snapshot()
            rows(s)[0].update(change)
            saved = deepcopy(rows(s))
            warnings = plaster.apply(s, create=True)
            self.assertEqual(rows(s), saved)
            self.assertTrue(warnings['standard'])

    def test_recalculation_protects_manual_values_prices_exclusions_and_deleted_rows(self):
        s = snapshot()
        plaster.apply(s, create=True)
        rows(s)[0].update(quantity='12', auto_quantity=False, price='999', excluded=True, reason='Пользователь')
        del rows(s)[2]
        s['inputs']['length'] = '9'
        plaster.apply(s)
        self.assertEqual(rows(s)[0]['quantity'], '12')
        self.assertEqual(rows(s)[0]['price'], '999')
        self.assertEqual(rows(s)[0]['reason'], 'Пользователь')
        self.assertTrue(rows(s)[0]['excluded'])
        self.assertEqual(len(rows(s)), 2)
        self.assertGreater(D(rows(s)[1]['quantity']), D('86.608'))

    def test_units_unknown_geometry_and_thickness_never_charge_stale_auto_quantity(self):
        for change in ({'bowl_height': None}, {'tech_plaster_thickness': '.031'}, {'tech_plaster': ''}):
            s = snapshot()
            plaster.apply(s, create=True)
            s['inputs'].update(change)
            plaster.apply(s)
            self.assertIsNone(rows(s)[0]['quantity'])
            self.assertIsNone(rows(s)[1]['quantity'])
        self.assertIsNone(plaster.metrics({**INPUTS, 'tech_plaster_thickness': '.007'}, 'standard')['mix_net_kg'])
        s = snapshot()
        plaster.apply(s, create=True)
        rows(s, 'premium')[1].update(unit='кг', price='100')
        plaster.apply(s)
        self.assertIsNone(rows(s, 'premium')[1]['quantity'])
        self.assertEqual(rows(s, 'premium')[1]['price'], '100')

    def test_off_and_restore_preserve_user_exclusions(self):
        s = snapshot()
        plaster.apply(s, create=True)
        rows(s)[1].update(excluded=True, reason='Пользователь')
        s['inputs']['tech_plaster'] = 'off'
        plaster.apply(s)
        self.assertTrue(all(r['excluded'] for r in rows(s)))
        s['inputs']['tech_plaster'] = 'walls_floor'
        plaster.apply(s)
        self.assertFalse(rows(s)[0]['excluded'])
        self.assertTrue(rows(s)[1]['excluded'])
        self.assertEqual(rows(s)[1]['reason'], 'Пользователь')

    def test_walls_only_and_financial_quantity_uses_purchased_bags_once(self):
        values = plaster.metrics({**INPUTS, 'tech_plaster': 'walls'}, 'standard')
        self.assertEqual(values['area'], D('39.6'))
        s = snapshot()
        plaster.apply(s, create=True)
        rows(s)[0]['price'] = '100'  # Synthetic unit price, not catalogue data.
        rows(s)[1]['price'] = '10'
        before = D(rows(s)[0]['quantity']) * 100 + D(rows(s)[1]['quantity']) * 10
        self.assertEqual(before, D('10566.080'))
        plaster.apply(s)
        self.assertEqual(D(rows(s)[0]['quantity']) * 100 + D(rows(s)[1]['quantity']) * 10, before)


class PlasterIntegrationTests(SimpleTestCase):
    def test_fresh_roundtrip_recalculate_exact_totals_and_history(self):
        from pool_service.services.pool_estimates import generate, evaluate, recalculate
        from .test_pool_estimates import INPUTS as CORE
        def material(s,tier):
            return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['id']=='tech_plaster_material')
        s=generate({**CORE,**INPUTS})
        self.assertEqual(material(s,'standard')['quantity'],'97')
        self.assertEqual(material(s,'standard')['unit'],'меш.')
        material(s,'standard')['price']='100'
        saved=evaluate(s)
        self.assertEqual(material(saved,'standard')['amount'],'9700.00')
        again=recalculate(saved,saved['inputs'])
        self.assertEqual(material(again,'standard')['amount'],'9700.00')
        self.assertIn('2291.2',material(again,'standard')['construction_quantity_note'])
        self.assertEqual(len([r for b in again['variants']['standard']['blocks'] for r in b['rows'] if r['id']=='tech_plaster_latex']),1)
        old=deepcopy(saved);old.pop('plaster_systems')
        # A genuine old volume row is not migrated or repriced on recalculation.
        for v in old['variants'].values():
            for b in v['blocks']:
                b['rows']=[r for r in b['rows'] if r['id'] not in ('tech_plaster_latex','tech_plaster_contact')]
        material(old,'standard').update(unit='м³',basis='tech_plaster_volume',quantity='2.000',price='123',auto_quantity=False)
        result=recalculate(old,old['inputs'])
        self.assertEqual(material(result,'standard')['amount'],'246.00')
        self.assertEqual(material(result,'standard')['unit'],'м³')


    def test_economy_separate_wall_and_floor_stock_purchase(self):
        s = snapshot()
        warnings = plaster.apply(s, create=True)
        values = plaster.economy_metrics(INPUTS)
        self.assertEqual(values['plaster_wall_bags']['net'], D('1322.64'))
        self.assertEqual(values['plaster_wall_bags']['stock'], D('1388.772'))
        self.assertEqual(values['plaster_floor_bags']['net'], D('1280'))
        self.assertEqual(values['plaster_floor_bags']['stock'], D('1344'))
        self.assertEqual([r['quantity'] for r in rows(s, 'economy')], ['56', '45', None])
        self.assertTrue(all(r['price'] is None for r in rows(s, 'economy')))
        self.assertIn('не менее 20 МПа', warnings['economy'][0])
        self.assertIn('ПВХ не подтверждена', warnings['economy'][0])
        self.assertFalse(any('GH-106' in r['name'] for r in rows(s, 'economy')))
        saved = deepcopy(s)
        plaster.apply(s, create=True)
        self.assertEqual(s, saved)

    def test_economy_independent_thickness_limits(self):
        for mm, wall, floor in [('4', False, False), ('5', True, False), ('19', True, False),
                               ('20', True, True), ('30', True, True), ('31', False, True),
                               ('100', False, True), ('101', False, False)]:
            values = plaster.economy_metrics({**INPUTS, 'tech_plaster_thickness': str(D(mm)/1000)})
            self.assertEqual(values['plaster_wall_bags']['bags'] is not None, wall)
            self.assertEqual(values['plaster_floor_bags']['bags'] is not None, floor)

    def test_economy_walls_only_floor_exclusion_and_manual_protection(self):
        s = snapshot()
        plaster.apply(s, create=True)
        s['inputs']['tech_plaster'] = 'walls'
        plaster.apply(s)
        self.assertFalse(rows(s, 'economy')[0]['excluded'])
        self.assertTrue(rows(s, 'economy')[1]['excluded'])
        s['inputs']['tech_plaster'] = 'walls_floor'
        plaster.apply(s)
        self.assertFalse(rows(s, 'economy')[1]['excluded'])
        rows(s, 'economy')[1].update(auto_quantity=False, quantity='7', price='900', excluded=True, reason='Пользователь')
        s['inputs']['length'] = '10'
        del rows(s, 'economy')[2]
        plaster.apply(s)
        self.assertEqual(len(rows(s, 'economy')), 2)
        self.assertEqual(rows(s, 'economy')[1]['quantity'], '7')
        self.assertEqual(rows(s, 'economy')[1]['price'], '900')
        self.assertEqual(rows(s, 'economy')[1]['reason'], 'Пользователь')

    def test_economy_manual_placeholder_not_replaced(self):
        s = snapshot()
        rows(s, 'economy')[0].update(price='123', unit_price_manual=True)
        saved = deepcopy(rows(s, 'economy'))
        plaster.apply(s, create=True)
        self.assertEqual(rows(s, 'economy'), saved)
