"""Display-only formatting; snapshot decimals and totals remain untouched."""
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from django import template
register = template.Library()

@register.filter
def estimate_exact(value):
    if value is None or value == '':
        return ''
    try:
        number = Decimal(str(value).replace(',', '.'))
        if not number.is_finite():
            return str(value)
        text = format(number, 'f')
        return text.rstrip('0').rstrip('.') if '.' in text else text
    except InvalidOperation:
        return str(value)

@register.filter
def estimate_number(value):
    if value is None or value == '':
        return ''
    try:
        return estimate_exact(Decimal(str(value)).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)).replace('.', ',')
    except InvalidOperation:
        return str(value)

@register.filter
def estimate_quantity(value, unit):
    if re.fullmatch(r'(?:шт\.?|компл\.?|комплект(?:а|ов)?|упак\.?|пара|пар)', str(unit), re.I):
        return estimate_exact(value).replace('.', ',')
    return estimate_number(value)

@register.filter
def estimate_note(value):
    return re.sub(r'(?<![\w/])\d+\.\d+', lambda m: estimate_exact(m[0]), str(value))

@register.filter
def estimate_money(value):
    if value is None or value == '': return ''
    try: return format(Decimal(str(value)).quantize(Decimal('1'), rounding=ROUND_HALF_UP), 'f')
    except InvalidOperation: return str(value)

@register.filter
def estimate_sections(variant):
    from pool_service.services.estimate_layout import sections
    return sections(variant, client=True)

@register.filter
def estimate_variants(variants):
    return [(key,variants[key]) for key in ('economy','standard','premium') if key in variants]
