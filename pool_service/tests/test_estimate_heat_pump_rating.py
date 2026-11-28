import json
import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal

from pool_service.services.estimate_heat_pump_rating import PARAM, auto_block, normalize_rating, parse_rating, rating_note
from pool_service.services.estimate_candidates import assess


def offer(raw, sku='45959', name='Тепловой насос для бассейна'):
    x = ET.Element('offer')
    ET.SubElement(x, 'vendorCode').text = sku
    ET.SubElement(x, 'name').text = name
    ET.SubElement(x, 'param', name=PARAM).text = json.dumps(raw)
    return x


class HeatPumpRatingTests(unittest.TestCase):
    exact = {'sku':'45959','kw':'3.5','air_c':26,'water_c':26,
             'rh_pct':'80','mode':'on_off','source':'catalog:heating_output_at_a26_w26'}
    fallback = {'sku':'28766','kw':'35.2','air_c':27,'water_c':26,'water_outlet_c':'28',
                'air_wet_bulb_c':'24.3','mode':'inverter maximum',
                'source':'https://aquapolis.ru/media/custom_css/manuals/instructions/aquaviva_vertical.pdf'}

    def test_round_trip(self):
        self.assertEqual(parse_rating(ET.fromstring(ET.tostring(offer(self.exact)))),self.exact)
        self.assertEqual(parse_rating(offer(self.fallback, '28766')), self.fallback)
        self.assertIn('воздухе +27 °C',rating_note({'pool_heat_pump_rating':self.fallback}))

    def test_missing_or_invalid(self):
        for broken in ({**self.exact,'kw':'NaN'},{**self.exact,'kw':'0'},
                       {**self.exact,'kw':3.5},{**self.exact,'air_c':15},
                       {**self.exact,'air_c':27},{**self.exact,'water_c':27},
                       {**self.exact,'sku':'45958'}):
            self.assertIsNone(parse_rating(offer(broken)))
        x=offer(self.exact)
        x.append(x.find('param'))
        self.assertIsNone(parse_rating(x))
        self.assertIsNone(parse_rating(offer(self.exact,name='Тепловой насос ГВС')))
        self.assertIsNone(normalize_rating([], '45959'))

    def test_auto_sizing_condition(self):
        self.assertIsNone(auto_block({'pool_heat_pump_rating':self.exact},{'power':'3.5','mode':'manual'}))
        self.assertTrue(auto_block({'pool_heat_pump_rating':self.exact},{'power':'3.5','mode':'estimated'}))
        self.assertTrue(auto_block({'heating_power':'13.5'},{'power':'13','mode':'manual'}))

    def test_threshold_and_unaffected_heater_types(self):
        base={'kind':'heater','heater_type':'heatpump','available':True,'heating_power':'3.5',
              'pool_heat_pump_rating':self.exact,'power_options':['230']}
        inputs={'heating':'heatpump','heating_power':'3.5','power':'230'}
        self.assertIsNone(assess(base,inputs,Decimal(8))[0])
        self.assertIsNotNone(assess(base,{**inputs,'heating_power':'3.6'},Decimal(8))[0])
        self.assertIsNotNone(assess({**base,'pool_heat_pump_rating':None},inputs,Decimal(8))[0])
        for h in ['electric','exchanger']:
            self.assertIsNone(assess({**base,'heater_type':h,'pool_heat_pump_rating':None},{**inputs,'heating':h},Decimal(8))[0])
