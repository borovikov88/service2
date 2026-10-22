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
    from pool_service.services.estimate_works import is_work
    sections=[]
    for title, mode in [('Материалы и оборудование','material'),('Этап 1. Строительство чаши — работы','construction'),('Этап 2. Монтаж оборудования и отделка — работы','installation')]:
        blocks=[]
        for block in variant['blocks']:
            if block['excluded']: continue
            rows=[r for r in block['rows'] if not r['excluded'] and not r['existing'] and
                  (not is_work(r) if mode=='material' else is_work(r) and (block['id']=='construction' and not re.search(r'transport|travel|handling|delivery',r.get('work_code',''))) == (mode=='construction'))]
            if rows: blocks.append({**block,'rows':rows,'subtotal':sum((Decimal(r['amount']) for r in rows if r.get('amount') is not None),Decimal(0))})
        if blocks: sections.append({'title':title,'blocks':blocks})
    return sections
