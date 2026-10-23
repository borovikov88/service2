"""Charge displayed quantities and whole-rouble prices; retain exact source values."""
from decimal import Decimal, ROUND_HALF_UP
import re

PIECES = re.compile(r'(?:шт\.?|компл\.?|комплект(?:а|ов)?|упак\.?|пара|пар)', re.I)


def quantity(value, unit):
    if value is None:
        return None
    value = Decimal(value)
    # Historical fractional pieces are displayed in full, like the UI/print.
    return value if PIECES.fullmatch(unit.strip()) else value.quantize(Decimal('.1'), rounding=ROUND_HALF_UP)


def price(value):
    return None if value is None else Decimal(value).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def amount(row):
    count, rate = quantity(row['quantity'], row['unit']), price(row['price'])
    row['calculation_quantity'] = str(count) if count is not None else None
    row['calculation_price'] = str(rate) if rate is not None else None
    return None if count is None or rate is None else (count * rate).quantize(Decimal('1'), rounding=ROUND_HALF_UP).quantize(Decimal('.01'))


def notes(row):
    for field in ('price_conversion_note', 'construction_price_note'):
        note = row.get(field, '')
        if note:
            note = note.replace('сумма рассчитана без промежуточного округления', 'сумма рассчитана по показанным количеству и цене')
            note = note.replace('сумма без промежуточного округления', 'сумма по показанным количеству и цене')
            note = note.replace('Расчёт без промежуточного округления цены.', 'Для суммы используется показанная цена в целых рублях.')
            row[field] = note
