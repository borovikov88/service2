"""Synthetic dosing/control proposals, no live supplier requests or prices."""
from copy import deepcopy
from decimal import Decimal
import json
from xml.etree import ElementTree as ET

from django.test import SimpleTestCase
from pool_service.services import estimate_catalog as catalog, estimate_auxiliary as aux
from pool_service.services.pool_estimates import generate, evaluate, recalculate, make_row, EstimateInputError
from .test_estimate_catalog import feed, offer
from .test_pool_estimates import INPUTS, block


def row(snapshot,kind,tier='economy'):
    return next(r for b in snapshot['variants'][tier]['blocks'] for r in b['rows'] if r['kind']==kind)


def fixture(*extra):
    return catalog.parse_catalog(feed(
        offer(1,'Хлоратор-полуавтомат Synthetic 4 кг','100',description='Для бассейнов объемом до 100 м3'),
        offer(2,'Система дозирующих насосов Synthetic PH/RX 5л/ч + Измерительный набор','200',description=''),
        offer(3,'Система дозирующих насосов Synthetic PH/RX+Free Cl 5л/ч + Ячейка','300',description=''),
        offer(4,'Панель управления фильтрацией Synthetic 230В 14A','50',description='Мощность насоса до 2 кВт'),
        offer(5,'Насос Synthetic 8m3/h*8m 220V 1 кВт 5A','80'),*extra))


class AuxiliaryTests(SimpleTestCase):
    def test_purpose_identity_does_not_replace_measurement_with_rx_or_accessories(self):
        products=fixture(offer(6,'Панель управления дозацией K800 PH-RX-CL','1'),
            offer(7,'Перистальтический дозирующий насос PH/RX','1'),
            offer(8,'Тестер PH/CL','1'),offer(9,'Ячейка PH/RX+Free Cl','1'),
            offer(10,'Хлоргенератор Synthetic 20 г/ч','1'),
            offer(11,'Панель управления аттракционами Synthetic','1'),
            offer(12,'Панель управления доливом Synthetic','1'))['products']
        self.assertEqual([products[str(i)]['kind'] for i in (1,2,3,4)],['treatment']*3+['control_panel'])
        self.assertEqual(products['2']['auxiliary']['mode'],'rx_ph')
        self.assertEqual(products['3']['auxiliary']['mode'],'cl_ph')
        self.assertTrue(products['3']['auxiliary']['free_cl'])
        self.assertTrue(all(products[str(i)]['kind'] not in aux.TITLES for i in range(6,13)))
        snapshot=generate({**INPUTS,'power':'230'})
        self.assertIsNotNone(aux.assess(products['2'],snapshot,snapshot['variants']['premium'],'premium')[0])
        self.assertIsNone(aux.assess(products['3'],snapshot,snapshot['variants']['premium'],'premium')[0])

    def test_tier_modes_control_limits_and_exact_new_totals(self):
        original=generate({**INPUTS,'power':'230','required_head':'8'})
        result,_=catalog.auto_select(original,fixture())
        for tier,pid,total in [('economy','1','230'),('standard','2','330'),('premium','3','430')]:
            self.assertEqual(row(result,'treatment',tier)['catalog']['id'],pid)
            self.assertEqual(row(result,'control_panel',tier)['catalog']['id'],'4')
            self.assertEqual(Decimal(result['variants'][tier]['total'])-Decimal(original['variants'][tier]['total']),Decimal(total))
            self.assertIn('Проверить',row(result,'treatment',tier)['selection_note'])
            self.assertIn('протоку',row(result,'treatment',tier)['selection_note'])
            self.assertIn('схема защиты',row(result,'control_panel',tier)['selection_note'])
        twice,count=catalog.auto_select(evaluate(json.loads(json.dumps(result))),fixture())
        self.assertEqual(count,0)
        self.assertEqual(twice['variants'],result['variants'])

    def test_known_bad_volume_voltage_and_panel_load_are_rejected(self):
        c=fixture(offer(6,'Хлоратор-полуавтомат Small','1',description='Для бассейнов объемом до 10 м3'),
            offer(7,'Панель управления фильтрацией Small 230В 2A','1',description='Мощность насоса до 0.5 кВт'),
            offer(8,'Панель управления фильтрацией Wrong 380В 20A','1',description=''),
            offer(9,'Система дозирующих насосов PH/RX без датчиков','1',description=''))
        result,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),c)
        self.assertEqual(row(result,'treatment')['catalog']['id'],'1')
        for tier in result['variants']:
            self.assertEqual(row(result,'control_panel',tier)['catalog']['id'],'4')
        self.assertEqual(row(result,'treatment','standard')['catalog']['id'],'2')
        wrong=catalog.parse_catalog(feed(offer(20,'Станция дозирования PH/RX для питьевой воды','1',description='')))
        blocked,_=catalog.auto_select(generate(INPUTS),wrong)
        self.assertIsNone(row(blocked,'treatment','standard')['catalog'])
        allowed=catalog.parse_catalog(feed(offer(20,'Станция дозирования PH/RX для бассейна','1',description='Для питьевой воды не подходит.')))
        accepted,_=catalog.auto_select(generate(INPUTS),allowed)
        self.assertEqual(row(accepted,'treatment','standard')['catalog']['id'],'20')

    def test_unknown_station_has_specific_warning_but_known_rx_never_premium(self):
        c=catalog.parse_catalog(feed(offer(2,'Станция дозирования Synthetic','200',description=''),
            offer(3,'Станция дозирования Synthetic PH/RX','1',description='')))
        snapshot=generate(INPUTS)
        result,_=catalog.auto_select(snapshot,c)
        self.assertIsNone(row(result,'treatment')['catalog'])
        self.assertEqual(row(result,'treatment','standard')['catalog']['id'],'3')
        self.assertEqual(row(result,'treatment','premium')['catalog']['id'],'2')
        self.assertIn('измерение свободного',row(result,'treatment','premium')['selection_note'])
        self.assertIn('метод измерения',row(result,'treatment','premium')['selection_note'])

    def test_conflicting_parameters_and_multiple_pumps_need_no_guess(self):
        specs={'kind':'control_panel'}
        xml=ET.fromstring('<offer><param name="Максимальная мощность насоса">3 кВт</param></offer>')
        aux.classify(specs,'Панель управления фильтрацией Synthetic 230В','Мощность насоса до 2 кВт',xml)
        self.assertTrue(specs['auxiliary']['conflict'])
        specs={'kind':'other','power_options':['230']}
        xml=ET.fromstring('<offer><param name="Измеряемые параметры">pH/Cl</param><param name="Напряжение питания">380 В</param></offer>')
        aux.classify(specs,'Станция дозирования PH/RX 230В','',xml)
        self.assertTrue(specs['auxiliary']['conflict'])
        snapshot,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),fixture())
        block(snapshot)['rows'].append(dict(deepcopy(row(snapshot,'pump')),id='pump_two'))
        result,_=catalog.auto_select(snapshot,fixture())
        self.assertIsNone(row(result,'control_panel')['catalog'])
        self.assertIn('нескольких',row(result,'control_panel')['selection_note'])
        with self.assertRaises(EstimateInputError): aux.normalize_facts({'max_kw':'NaN'})
        with self.assertRaises(EstimateInputError): aux.normalize_facts({'pump_count':'1.5'})

    def test_manual_excluded_old_versions_and_frozen_price_are_preserved(self):
        initial,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),fixture())
        current=fixture();current['products']['2']['price']='999'
        refreshed,_=catalog.auto_select(initial,current)
        self.assertEqual(row(refreshed,'treatment','standard')['price'],'200.00')
        self.assertEqual(row(refreshed,'treatment','standard')['catalog']['price'],'200.00')
        manual=deepcopy(initial);r=row(manual,'control_panel')
        r.update(price='123.45',unit_price_manual=True,quantity='2')
        current['products']['4']['auxiliary']['max_kw']='0.5'
        kept,_=catalog.auto_select(manual,current)
        self.assertEqual(row(kept,'control_panel')['price'],'123.45')
        self.assertEqual(row(kept,'control_panel')['quantity'],'2.000')
        self.assertIn('Текущая выгрузка',row(kept,'control_panel')['selection_note'])
        self.assertEqual(row(kept,'control_panel')['amount'],'246.90')
        excluded=deepcopy(initial);row(excluded,'treatment').update(excluded=True,reason='Ручное исключение')
        kept,_=catalog.auto_select(excluded,current)
        self.assertTrue(row(kept,'treatment')['excluded'])
        old=deepcopy(initial);old.pop('auxiliary_version')
        before=deepcopy(old)
        self.assertEqual(aux.fill(old,current),0)
        self.assertEqual(old,before)

    def test_xml_units_define_limits_and_reject_undersized_panel(self):
        panel=offer(20,'Панель управления фильтрацией Small 230В','1',description='').replace('</offer>','<param name="Максимальная мощность насоса" unit="кВт">0.5</param></offer>')
        source=fixture(panel)
        self.assertEqual(source['products']['20']['auxiliary']['max_kw'],'0.5')
        result,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),source)
        for tier in result['variants']:
            self.assertEqual(row(result,'control_panel',tier)['catalog']['id'],'4')
        for label,unit,value,key in [
            ('Максимальный объем бассейна','м³','10','max_volume'),
            ('Максимальный ток насоса','A','2','max_current'),
            ('Количество насосов','шт','2','pump_count'),
            ('Мощность двигателя','кВт','1','motor_kw'),
            ('Номинальный ток двигателя','А','5','motor_current'),
        ]:
            with self.subTest(label=label):
                specs={'kind':'control_panel'}
                xml=ET.fromstring(f'<offer><param name="{label}" unit="{unit}">{value}</param></offer>')
                aux.classify(specs,'Панель управления фильтрацией Synthetic','',xml)
                self.assertEqual(specs['auxiliary'][key],value)
        specs={'kind':'control_panel'}
        xml=ET.fromstring('<offer><param name="Максимальная мощность насоса" unit="Вт">0.5</param></offer>')
        aux.classify(specs,'Панель управления фильтрацией Synthetic','',xml)
        self.assertTrue(specs['auxiliary']['conflict'])

    def test_negated_chlorine_measurement_never_becomes_premium(self):
        cases=[
            ('Станция дозирования PH/RX без измерения свободного хлора','',''),
            ('Станция дозирования PH/RX','Без измерения свободного хлора, только Rx.',''),
            ('Станция дозирования PH/RX','Не измеряет pH, Rx и свободный хлор.',''),
            ('Станция дозирования PH/RX','Измерение свободного хлора не предусмотрено.',''),
            ('Станция дозирования PH/RX','','<param name="Измеряемые параметры">pH/Rx; без измерения свободного хлора</param>'),
            ('Станция дозирования PH/RX','','<param name="Измерение свободного хлора">нет</param>'),
            ('Станция дозирования PH/Free Cl','Без измерения свободного хлора, только Rx.',''),
            ('Станция дозирования PH/RX без измерения свободного хлора','','<param name="Измеряемые параметры">pH/Free Cl</param>'),
        ]
        for title,description,params in cases:
            with self.subTest(title=title,description=description,params=params):
                item=offer(20,title,'1',description=description).replace('</offer>',params+'</offer>')
                source=fixture(item)
                facts=source['products']['20']['auxiliary']
                self.assertTrue(facts['chlorine_denied'])
                self.assertFalse(facts['free_cl'])
                initial=generate({**INPUTS,'power':'230'})
                result,_=catalog.auto_select(initial,source)
                self.assertEqual(row(result,'treatment','premium')['catalog']['id'],'3')
                again,_=catalog.auto_select(recalculate(evaluate(result),result['inputs']),source)
                self.assertEqual(row(again,'treatment','premium')['catalog']['id'],'3')
        confirmed=fixture()['products']['3']['auxiliary']
        self.assertTrue(confirmed['free_cl'])
        self.assertFalse(confirmed['chlorine_denied'])
        self.assertFalse(confirmed['conflict'])

    def test_chlorine_denial_is_local_and_conflicts_survive_normalization(self):
        good=offer(20,'Система дозирующих насосов PH/RX+Free Cl','1',description='Без измерения температуры, измерение свободного хлора предусмотрено. Не только Rx, но и Cl.')
        source=catalog.parse_catalog(feed(good))
        result,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),source)
        self.assertEqual(row(result,'treatment','premium')['catalog']['id'],'20')
        self.assertFalse(source['products']['20']['auxiliary']['chlorine_denied'])
        bad=offer(20,'Станция дозирования PH/Free Cl','1',description='Без измерения свободного хлора, только Rx.')
        facts=fixture(bad)['products']['20']['auxiliary']
        self.assertTrue(facts['conflict'])
        self.assertTrue(aux.normalize_facts(facts)['chlorine_denied'])

    def test_missing_chlorine_sensor_or_dosing_rejects_incomplete_station(self):
        base='Станция дозирования PH/RX+Free Cl'
        cases=[
            (base,'Комплект не включает датчик свободного хлора.','','chlorine_sensor_missing'),
            (base,'Без дозирования хлора, только pH.','','chlorine_dosing_denied'),
            (base+' без датчика свободного хлора','','','chlorine_sensor_missing'),
            (base+' без дозирования хлора','','','chlorine_dosing_denied'),
            (base,'Комплект не включает датчики pH, Rx и свободного хлора.','','chlorine_sensor_missing'),
            (base,'Датчики pH, Rx и свободного хлора отсутствуют.','','chlorine_sensor_missing'),
            (base,'Не поддерживает дозирование pH и хлора.','','chlorine_dosing_denied'),
            (base,'Датчик свободного хлора не входит в комплект.','','chlorine_sensor_missing'),
            (base,'Дозирование хлора не предусмотрено.','','chlorine_dosing_denied'),
            (base,'Не дозирует хлор.','','chlorine_dosing_denied'),
            (base,'','<param name="Комплектация">Комплект не включает датчик свободного хлора.</param>','chlorine_sensor_missing'),
            (base,'','<param name="Датчик свободного хлора">нет</param>','chlorine_sensor_missing'),
            (base,'','<param name="Дозирование хлора">отсутствует</param>','chlorine_dosing_denied'),
        ]
        for title,description,params,key in cases:
            with self.subTest(title=title,description=description,params=params):
                item=offer(20,title,'1',description=description).replace('</offer>',params+'</offer>')
                source=catalog.parse_catalog(feed(item))
                facts=source['products']['20']['auxiliary']
                self.assertTrue(facts[key])
                self.assertTrue(aux.normalize_facts(facts)[key])
                if key=='chlorine_sensor_missing': self.assertTrue(facts['incomplete'])
                result,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),source)
                self.assertIsNone(row(result,'treatment','premium')['catalog'])
                reason=aux.search_reason(source['products']['20'],result['inputs'],Decimal('8'),'premium')
                self.assertIn('датчик' if key=='chlorine_sensor_missing' else 'дозирования',reason)
                again,_=catalog.auto_select(recalculate(evaluate(result),result['inputs']),source)
                self.assertIsNone(row(again,'treatment','premium')['catalog'])

    def test_complete_chlorine_station_and_unrelated_missing_component_remain_eligible(self):
        for description in [
            'Комплект включает датчик свободного хлора. Дозирование хлора предусмотрено.',
            'Без датчика температуры, измеряет свободный хлор.',
            'Датчик свободного хлора включен, датчик температуры отсутствует.',
            'Комплект не включает датчик температуры, но включает датчик свободного хлора.',
            'Без дозирования альгицида, дозирование хлора предусмотрено.',
            'Датчик температуры не входит в комплект. Датчик свободного хлора включен.',
        ]:
            with self.subTest(description=description):
                source=catalog.parse_catalog(feed(offer(20,'Система дозирующих насосов PH/RX+Free Cl','1',description=description)))
                facts=source['products']['20']['auxiliary']
                self.assertFalse(facts['chlorine_sensor_missing'])
                self.assertFalse(facts['chlorine_dosing_denied'])
                result,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),source)
                self.assertEqual(row(result,'treatment','premium')['catalog']['id'],'20')

    def test_rx_station_needs_rx_sensor_not_optional_chlorine_sensor(self):
        for description in ['Датчик свободного хлора не входит в комплект.', 'Без датчика свободного хлора.']:
            with self.subTest(description=description):
                source=catalog.parse_catalog(feed(offer(20,'Система дозирующих насосов PH/RX','1',description=description)))
                facts=source['products']['20']['auxiliary']
                self.assertTrue(facts['chlorine_sensor_missing'])
                self.assertFalse(facts['incomplete'])
                result,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),source)
                self.assertEqual(row(result,'treatment','standard')['catalog']['id'],'20')
                self.assertIsNone(row(result,'treatment','premium')['catalog'])
        for description in ['Без датчиков pH и Rx.', 'Без дозирования хлора, только pH.']:
            with self.subTest(description=description):
                source=catalog.parse_catalog(feed(offer(20,'Система дозирующих насосов PH/RX','1',description=description)))
                result,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),source)
                self.assertIsNone(row(result,'treatment','standard')['catalog'])

    def test_component_assertions_keep_negation_local_for_each_separator(self):
        for separator in [', ', '; ', '. ', ' и ']:
            for left,right,eligible in [
                ('Без датчика температуры','датчик свободного хлора включен',True),
                ('Датчик температуры включён','датчика хлора нет',False),
                ('Датчик свободного хлора включён','датчика температуры нет',True),
            ]:
                with self.subTest(separator=separator,left=left,right=right):
                    description=left+separator+right+'.'
                    source=catalog.parse_catalog(feed(offer(20,'Станция дозирования PH/RX+Free Cl','1',description=description)))
                    facts=source['products']['20']['auxiliary']
                    self.assertEqual(facts['chlorine_sensor_missing'],not eligible)
                    if eligible:
                        self.assertFalse(facts['chlorine_denied'])
                        self.assertFalse(facts['incomplete'])
                        self.assertTrue(facts['free_cl'])
                    result,_=catalog.auto_select(generate({**INPUTS,'power':'230'}),source)
                    self.assertEqual(row(result,'treatment','premium')['catalog'] is not None,eligible)
