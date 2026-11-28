"""Temperature-qualified pool heat-pump comparison point from the shop feed."""
from decimal import Decimal
import json
import re
from urllib.parse import urlsplit

PARAM = 'pool_heat_pump_rating_v1'


def normalize_rating(raw, sku):
    if not isinstance(raw, dict) or not isinstance(sku, str) or not re.fullmatch(r'[0-9]{1,16}', sku) or raw.get('sku') != sku:
        return None
    kw, air, water, source = (raw.get(k) for k in ('kw', 'air_c', 'water_c', 'source'))
    if (not isinstance(kw, str) or not re.fullmatch(r'[0-9]{1,3}(?:\.[0-9]{1,2})?', kw)
            or not Decimal(0) < Decimal(kw) <= Decimal(200)
            or type(air) is not int or type(water) is not int
            or air not in (26, 27) or water not in (26, 27) or air == 26 and water != 26
            or not isinstance(source, str) or not 0 < len(source) <= 800):
        return None
    if not (source == 'catalog:heating_output_at_a26_w26' and air == 26):
        try:
            url = urlsplit(source)
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment or any(c.isspace() for c in source):
                return None
        except ValueError:
            return None
    out = dict(sku=sku, kw=kw, air_c=air, water_c=water, source=source)
    for key, valid in [('mode', {'turbo','on_off','Boost','inverter maximum'}),
                       ('rh_pct', {'78','80'}), ('water_outlet_c', {'28'}), ('air_wet_bulb_c', {'24.3'})]:
        value = raw.get(key)
        if value is not None:
            if not isinstance(value, str) or value not in valid:
                return None
            out[key] = value
    return out


def parse_rating(offer):
    params = [p.text or '' for p in offer.findall('param') if p.get('name') == PARAM]
    if len(params) != 1 or not 0 < len(params[0]) <= 1400:
        return None
    if re.search(r'\bГВС\b', offer.findtext('name', ''), flags=re.I):
        return None
    try:
        return normalize_rating(json.loads(params[0]), offer.findtext('vendorCode',''))
    except (ValueError, RecursionError):
        return None


def auto_block(product, requirement):
    rating = product.get('pool_heat_pump_rating')
    if not rating:
        return 'Нет подтверждённой тепловой мощности для бассейна при +26–27 °C воздуха.'
    if not requirement['power']:
        return 'Не задана требуемая тепловая мощность.'
    if requirement.get('mode') != 'manual':
        return 'Паспортная мощность при +26–27 °C не подтверждает мощность при расчётных условиях объекта. Нужна ручная проверка.'
    return None


def rating_note(product):
    r = product.get('pool_heat_pump_rating')
    if not r:
        return 'Мощность при +26–27 °C не подтверждена; нужен ручной подбор.'
    inlet = f"вода +{r['water_c']} °C"
    if r.get('water_outlet_c'):
        inlet += f" на входе / +{r['water_outlet_c']} °C на выходе"
    humidity = f", влажность {r['rh_pct']}%" if r.get('rh_pct') else ''
    wet = f" (+{r['air_wet_bulb_c']} °C по влажному термометру)" if r.get('air_wet_bulb_c') else ''
    mode = {'turbo':'Turbo','on_off':'On/Off','Boost':'Boost','inverter maximum':'максимум инвертора'}.get(r.get('mode'))
    return (f"Паспортный ориентир: {r['kw'].replace('.', ',')} кВт при воздухе +{r['air_c']} °C{wet}, "
            f"{inlet}{humidity}{', режим '+mode if mode else ''}. Проверьте мощность при условиях объекта.")
