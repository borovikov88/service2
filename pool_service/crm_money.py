"""Exact Decimal localization; document precision is not limited to kopecks."""
import re
from decimal import Decimal


def parse_money_input(value):
    text = str(value).strip()
    if not re.fullmatch(r'-?(?:[0-9]+|[0-9]{1,3}(?:[ \u00a0\u202f][0-9]{3})+)(?:[.,][0-9]+)?(?:[eE][+-]?[0-9]+)?', text):
        raise ValueError('Укажите сумму в формате 1 234,56.')
    return Decimal(re.sub(r'[ \u00a0\u202f]', '', text).replace(',', '.'))


def display_money(value):
    if value is None or value == '': return ''
    amount = Decimal(str(value))
    if not amount.is_finite(): raise ValueError('Nonfinite monetary amount')
    whole, _, fraction = format(amount, 'f').partition('.')
    grouped = re.sub(r'\B(?=(\d{3})+(?!\d))', '\u00a0', whole)
    return grouped + ',' + fraction.ljust(2, '0')
