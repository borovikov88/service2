from copy import deepcopy
from decimal import Decimal
from xml.etree import ElementTree as ET
from django.test import SimpleTestCase
from pool_service.services import pool_estimates as engine, estimate_catalog as catalog, estimate_temperature_control as temp
from .test_pool_estimates import INPUTS, block
from .test_estimate_catalog import feed, offer

HEAT={**INPUTS,'heating':'exchanger','heating_power':'20'}

def row(s,kind='thermostat',tier='economy'):
    return next(r for b in s['variants'][tier]['blocks'] for r in b['rows'] if r['kind']==kind)


def fixture(panel=False):
    offers=[offer(1,'Цифровой контроллер-термостат Aquaviva','100',url=temp.THERMOSTAT_URL,description='Для теплообменников бассейна.')]
    if panel:offers.append(offer(2,'Панель управления фильтрацией и нагревом Aquaviva AVP-01','500',url=temp.AVP_URL,description=''))
    return catalog.parse_catalog(feed(*offers))


class TemperatureControlTests(SimpleTestCase):
    def test_new_explicit_incompatibility_clears_auto_but_preserves_manual(self):
        data=fixture();s,_=catalog.auto_select(engine.generate(HEAT),data)
        changed=deepcopy(data);changed['products']['1']['temperature']['exchanger']='no'
        auto,_=catalog.auto_select(s,changed)
        self.assertIsNone(row(auto)['catalog']);self.assertIsNone(row(auto)['price'])
        self.assertIn('прямо исключает',row(auto)['selection_note'])
        row(s).update(unit_price_manual=True,price='77')
        manual,_=catalog.auto_select(s,changed)
        self.assertEqual(row(manual)['price'],'77.00')
        self.assertIn('прямо исключает',row(manual)['selection_note'])
        for description in ('Не подходит для теплообменников.','Не для бассейнов.'):
            data=catalog.parse_catalog(feed(offer(4,'Термостат для бассейна','100',description=description)))
            selected,_=catalog.auto_select(engine.generate(HEAT),data)
            self.assertIsNone(row(selected)['catalog'])

    def test_two_panels_do_not_remove_thermostat_and_new_panel_evidence_is_used(self):
        s,_=catalog.auto_select(engine.generate(HEAT),fixture(True))
        other=deepcopy(row(s,'control_panel'));other['id']='second_panel';block(s)['rows'].append(other)
        self.assertFalse(row(engine.evaluate(s))['excluded'])
        s=engine.generate(HEAT);data=fixture(True)
        catalog.apply_product(row(s,'control_panel'),data['products']['2'],data,'manual')
        s=engine.evaluate(s);self.assertTrue(row(s)['excluded'])
        data['products']['2']['temperature']['regulation']='no'
        changed,_=catalog.auto_select(s,data)
        self.assertFalse(row(changed)['excluded'])
        self.assertEqual(row(changed,'control_panel')['price'],'500.00')

    def test_exchanger_adds_one_per_quote_and_no_electric_only_thermostat(self):
        s,_=catalog.auto_select(engine.generate(HEAT),fixture())
        for tier in s['variants']:
            r=row(s,tier=tier)
            self.assertEqual((r['quantity'],r['price'],r['amount']),('1.000','100.00','100.00'))
            self.assertEqual(r['catalog']['id'],'1')
        for inputs in (INPUTS,{**HEAT,'heating':'electric'}):
            self.assertFalse(any(r['kind']=='thermostat' for b in engine.generate(inputs)['variants']['economy']['blocks'] for r in b['rows']))
        mixed=engine.generate({**HEAT,'heating_types':['electric','exchanger','heatpump']})
        self.assertEqual(sum(r['kind']=='thermostat' for b in mixed['variants']['economy']['blocks'] for r in b['rows']),1)

    def test_confirmed_panel_excludes_and_removal_restores_without_duplicate(self):
        s,_=catalog.auto_select(engine.generate(HEAT),fixture())
        initial=deepcopy(s)
        catalog.apply_product(row(s,'control_panel'),fixture(True)['products']['2'],fixture(True),'manual')
        s=engine.evaluate(s)
        self.assertTrue(row(s)['excluded']);self.assertEqual(row(s)['reason'],temp.PANEL)
        self.assertIsNone(row(s)['amount'])
        self.assertEqual(Decimal(s['variants']['economy']['total'])-Decimal(initial['variants']['economy']['total']),Decimal('400'))
        for _ in range(3):
            row(s,'control_panel')['excluded']=True;s=engine.recalculate(s,s['inputs'])
            self.assertFalse(row(s)['excluded']);self.assertEqual(row(s)['amount'],'100.00')
            row(s,'control_panel')['excluded']=False;s=engine.recalculate(s,s['inputs'])
            self.assertTrue(row(s)['excluded'])
        self.assertEqual(row(initial)['amount'],'100.00')

    def test_full_auto_panel_is_filled_before_thermostat_decision(self):
        s,_=catalog.auto_select(engine.generate(HEAT),fixture(True))
        for tier in s['variants']:
            self.assertEqual(row(s,'control_panel',tier)['catalog']['id'],'2')
            self.assertTrue(row(s,tier=tier)['excluded'])
        origin=row(s,'control_panel')['catalog'];origin.pop('temperature')
        self.assertTrue(row(engine.evaluate(s))['excluded']) # known old saved AVP model

    def test_capability_negative_display_only_and_xml_conflicts(self):
        for title,description,allowed in [
            ('Панель управления фильтрацией Synthetic','Только индикация температуры.',False),
            ('Панель управления фильтрацией Synthetic','Регулирование температуры. Управление теплообменником.',True),
            ('Панель управления фильтрацией Aquaviva AVP-010','',False),
            ('Панель управления фильтрацией Aquaviva AVP-01','Без датчика температуры.',False),
            ('Панель управления фильтрацией Aquaviva AVP-01','Не управляет теплообменником.',False),
            ('Панель управления фильтрацией Aquaviva AVP-01','Только индикация температуры.',False),
        ]:
            with self.subTest(title=title,description=description):
                data=catalog.parse_catalog(feed(offer(3,title,'500',description=description)))
                s=engine.generate(HEAT);catalog.apply_product(row(s,'control_panel'),data['products']['3'],data,'manual')
                self.assertEqual(row(engine.evaluate(s))['excluded'],allowed)
        specs={'kind':'control_panel'}
        temp.classify(specs,'Панель управления фильтрацией Aquaviva AVP-01',offer=ET.fromstring('<offer><param name="Регулирование температуры">нет</param></offer>'))
        self.assertTrue(specs['temperature']['conflict'])
        data=fixture(True);data['products']['2']['temperature']['conflict']=True
        selected,_=catalog.auto_select(engine.generate(HEAT),data)
        self.assertIsNone(row(selected,'control_panel')['catalog'])
        self.assertEqual(row(selected)['amount'],'100.00')

    def test_manual_price_zero_quantity_and_manual_exclusion_are_protected(self):
        for price in ('0',None,'555'):
            s,_=catalog.auto_select(engine.generate(HEAT),fixture())
            row(s).update(price=price,unit_price_manual=True,quantity='2',auto_quantity=False)
            catalog.apply_product(row(s,'control_panel'),fixture(True)['products']['2'],fixture(True),'manual')
            s=engine.evaluate(s)
            self.assertFalse(row(s)['excluded'])
            self.assertEqual(row(s)['price'],None if price is None else format(Decimal(price),'.2f'))
            self.assertIn('ручной термостат',row(s)['selection_note'])
        s=engine.generate(HEAT);row(s).update(excluded=True,reason='Исключено пользователем')
        s=engine.recalculate(s,s['inputs']);self.assertTrue(row(s)['excluded'])
        self.assertEqual(row(s)['reason'],'Исключено пользователем')

    def test_manual_unlinked_row_reused_and_deleted_row_stays_deleted(self):
        s=engine.generate(HEAT)
        r=row(s);r.update(heating_parent='',price='333',auto_quantity=False,quantity='3')
        s['variants']['economy']['thermostat_parents']=[]
        s=engine.recalculate(s,s['inputs'])
        self.assertEqual(row(s)['price'],'333.00');self.assertEqual(row(s)['quantity'],'3.000')
        self.assertEqual(sum(r['kind']=='thermostat' for b in s['variants']['economy']['blocks'] for r in b['rows']),1)
        for b in s['variants']['economy']['blocks']:b['rows']=[r for r in b['rows'] if r['kind']!='thermostat']
        for _ in range(3):s=engine.recalculate(s,s['inputs'])
        self.assertFalse(any(r['kind']=='thermostat' for b in s['variants']['economy']['blocks'] for r in b['rows']))

    def test_legacy_manual_other_thermostat_is_not_duplicated(self):
        s=engine.generate(HEAT);old=row(s)
        old.update(kind='other',name='Термостат',price='777',auto_quantity=False)
        s.pop('temperature_control_version');s['variants']['economy'].pop('thermostat_parents')
        updated=engine.recalculate(s,HEAT)
        self.assertFalse(any(r['kind']=='thermostat' for b in updated['variants']['economy']['blocks'] for r in b['rows']))
        saved=next(r for b in updated['variants']['economy']['blocks'] for r in b['rows'] if r['id']==old['id'])
        self.assertEqual(saved['price'],'777.00');self.assertIn('двойного начисления',saved['selection_note'])

    def test_parent_quantity_and_heating_off_restore(self):
        s=engine.generate(HEAT);row(s,'heater').update(quantity='2',auto_quantity=False)
        s=engine.evaluate(s);self.assertEqual(row(s)['quantity'],'2.000')
        off=engine.recalculate(s,{**s['inputs'],'heating':'none'})
        self.assertTrue(row(off)['excluded'])
        on=engine.recalculate(off,HEAT);self.assertFalse(row(on)['excluded'])
        self.assertEqual(row(on)['quantity'],'2.000')

    def test_no_history_upgrade_on_evaluate_and_no_unsafe_multiple_circuit_suppression(self):
        old=engine.generate(HEAT);old.pop('temperature_control_version')
        for v in old['variants'].values():
            v.pop('thermostat_parents',None)
            for b in v['blocks']:b['rows']=[r for r in b['rows'] if r['kind']!='thermostat']
        self.assertFalse(any(r['kind']=='thermostat' for b in engine.evaluate(old)['variants']['economy']['blocks'] for r in b['rows']))
        new=engine.recalculate(old,HEAT);self.assertEqual(row(new)['quantity'],'1.000')
        s=engine.generate(HEAT);other=deepcopy(row(s,'heater'));other['id']='second_exchanger';block(s)['rows'].append(other)
        s=engine.recalculate(s,HEAT)
        catalog.apply_product(row(s,'control_panel'),fixture(True)['products']['2'],fixture(True),'manual')
        s=engine.evaluate(s)
        self.assertFalse(any(r['excluded'] for b in s['variants']['economy']['blocks'] for r in b['rows'] if r['kind']=='thermostat'))
