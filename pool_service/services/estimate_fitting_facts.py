"""Narrow source-backed enrichment, never a price/availability override.

Aquaviva-branded PDF hosted by a dealer; direct manufacturer origin is unproven.
Emaux confirms model families, not Aquaviva SKU identity. Conflicting EM4414
flow figures (9 vs 5 m³/h) are deliberately NOT imported.
"""
import re
from html import unescape

SOURCE = 'https://aquaviva.nt-rt.ru/images/manuals/2-zakladnye-dlya-basseinov.pdf'
CORROBORATION = 'https://emauxgroup.com/products/pool-fittings/return-inlet/'
FACTS = {
    '16785': ('EM4412', 'film', '47–48', '88094612'),
    '16786': ('EM4414', 'film', '49–50', '91601224'),
    '16781': ('EM4408', 'concrete', '34–35', '91601212'),
    '16782': ('EM4409', 'concrete', '63–64', '88094312'),
}


def enrich(specs, name, brand, sku, description=""):
    fact = FACTS.get(sku)
    if not fact or brand.casefold() != 'aquaviva' or specs.get('embedded_role') != 'inlet':
        return
    model, lining, pages, article = fact
    if not re.search(r'(?<![a-z0-9_-])' + model + r'(?![a-z0-9_-])', name, re.I):
        return
    note = (f'Корпус ABS и отделка подтверждены для {model}, SKU {sku}, заводской артикул {article}: '
            f'каталог Aquaviva на сайте дилера ГКНТ, стр. {pages}; происхождение непосредственно от производителя не доказано. '
            'Расход этой модели не подтверждён; количество форсунок требует проверки по проекту.')
    if model == 'EM4414':
        note += ' В каталоге конфликт расхода: 9 и 5 м³/ч; ни одно значение автоматически не принято.'
    description = ' '.join(unescape(re.sub(r'<[^>]*>', ' ', description)).split())
    # Only explicit body/lining claims, not e.g. stainless screws or brass inserts.
    body_metal = re.search(r'(?:материал\s+корпуса|корпус)\s*[:—-]?\s*(?:из\s+)?(?:нержав|сталь|стали|бронз|алюмин|aisi|латун|металл)', description, re.I)
    opposite_lining = re.search(r'(?:для|под)\s+(?:бетон|плитк|мозаик)', description, re.I) if lining == 'film' else re.search(r'(?:для|под)\s+(?:лайнер|пл[её]нк)', description, re.I)
    conflict = bool(body_metal or opposite_lining) or bool(re.search(r'aisi|нерж', name, re.I)) or specs.get('material') not in ('', 'plastic') or bool(set(specs.get('lining', [])) - {lining})
    if conflict:
        specs.update(material='', lining=[], specification_conflict=True, specification_note='Конфликт характеристик выгрузки и каталога модели; автоматический подбор заблокирован. ' + note,
                     specification_source=SOURCE)
    else:
        specs.update(material='plastic', lining=[lining], specification_note=note, specification_source=SOURCE)
