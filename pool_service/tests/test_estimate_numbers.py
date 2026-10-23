from copy import deepcopy
from django.test import SimpleTestCase
from django.template.loader import get_template, render_to_string
from pool_service.templatetags.estimate_numbers import estimate_number, estimate_quantity, estimate_exact
from pool_service.services.pool_estimates import generate, evaluate, EstimateInputError
from .test_estimate_construction import NEW, row

class NumberDisplayTests(SimpleTestCase):
    def test_parameter_help_distinguishes_geometry_from_material_rounding(self):
        text = get_template('pool_service/estimates/calculator.html').template.source
        for phrase in (
            'Геометрия и объём работ по чаше считаются точно.',
            'В новых сметах автоматические количества песка (т) и бетона (м³) округляются вверх до целых',
            'арматуры — вверх до 0,5 т',
            'Точные ручные количества сохраняются; стоимость считается по показанным количеству и цене. Исторические версии не изменяются.',
        ):
            self.assertIn(phrase, text)
        self.assertRegex(text, r'<p class="pe-help" data-help-target="#pe-parameters-title">[^<]*арматуры — вверх до 0,5 т[^<]*</p>')
        self.assertNotIn('материалы считаются по расчётным объёмам без округления вверх', text)

    def test_display_only_print_and_exact_geometry(self):
        s=generate(NEW)
        for tier in s['variants']:
            row(s,'material_sand',tier).update(quantity='5.9136',auto_quantity=False)
            row(s,'material_rebar',tier).update(quantity='1.2592533',auto_quantity=False)
        s=evaluate(s); before=deepcopy(s)
        html=render_to_string('pool_service/estimates/print.html',{'version':{'snapshot':s,'number':1}})
        self.assertIn('5,9 т',html)
        self.assertIn('1,3 т',html)
        self.assertIn('6726',html)
        self.assertIn('117000',html)
        self.assertIn('0.15',html)
        self.assertEqual(s,before)
        for value in ('0.15','0.05','0.02'):self.assertEqual(estimate_exact(value),value)
        self.assertEqual(estimate_exact('.6000'),'0.6')
        self.assertEqual(estimate_quantity('2.000','шт'),'2')
        self.assertEqual(estimate_quantity('1.25','шт'),'1,25')
        self.assertEqual(estimate_number('20.000000'),'20')

    def test_new_manual_pieces_rejected_legacy_fraction_not_rewritten(self):
        s=generate(NEW)
        row(s,'pump').update(quantity='1.25',auto_quantity=False)
        self.assertEqual(row(evaluate(s),'pump')['quantity'],'1.250')
        row(s,'pump')['quantity_edited']=True
        with self.assertRaisesMessage(EstimateInputError,'целое количество'):evaluate(s)
        row(s,'pump')['quantity']='1.0001'
        with self.assertRaisesMessage(EstimateInputError,'целое количество'):evaluate(s)
        row(s,'pump')['quantity']='2'
        self.assertEqual(row(evaluate(s),'pump')['quantity'],'2.000')
        row(s,'material_sand').update(quantity='5.9136',auto_quantity=False,quantity_edited=True)
        self.assertEqual(row(evaluate(s),'material_sand')['amount'],'6726.00')
