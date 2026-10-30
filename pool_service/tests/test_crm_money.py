from decimal import Decimal
from django.test import SimpleTestCase
from pool_service.crm_money import parse_money_input, display_money

class CrmMoneyTests(SimpleTestCase):
    def test_exact_decimal_display_and_localized_input(self):
        for value in ('1234567.00','-1234.05','9999999999999999.99','0.00','120.005','0.005'):
            with self.subTest(value=value):
                self.assertEqual(parse_money_input(display_money(Decimal(value))), Decimal(value))
        self.assertEqual(display_money(Decimal('1234567')), '1\u00a0234\u00a0567,00')
        self.assertEqual(parse_money_input('1 234 567,00'), Decimal('1234567.00'))
        self.assertEqual(display_money(None), '')
        for bad in ('1 2', 'NaN', '1,234.00'):
            with self.assertRaises(ValueError): parse_money_input(bad)

    def test_document_precision_and_exponents_are_not_rounded(self):
        self.assertEqual(display_money('120.005'), '120,005')
        self.assertEqual(parse_money_input('0,005'), Decimal('0.005'))
        self.assertEqual(parse_money_input('1e-3'), Decimal('0.001'))
