"""Avito XML v3 serialization of the preserved reverse-export snapshot.

PriceList/Option structures follow the owner's Avito documentation examples.
Excel reverse-export metadata is never an input parameter of an XML ad.
"""
import re
from xml.etree import ElementTree as ET


SCALAR_FIELDS = set('''Id AvitoId ListingFee ManagerName ContactPhone Category Price
ContactMethod Title Description Address ServiceType ServiceSubtype Specialty
WorkExperience WorkTimeFrom WorkTimeTo ContactTimeFrom ContactTimeTo
WorkWithContract Guarantee MinimumOrderAmount MaterialPurchase MinimumAmountOfWork'''.split())
OPTION_FIELDS = set('''Payment TeamSize WorkDays ContactDays WaterBodies TypeOfPool
PoolWork TypeOfFountain FountainWork TypeOfPond ArtificialWaterWork AdditionalWork'''.split())
EXPORT_METADATA = {'AvitoStatus', 'AvitoDateEnd', 'CompanyName', 'EMail'}
PRICE_FIELDS = ('ServiceName', 'ServiceTitle', 'ServicePrice',
                'ServiceStartingPrice', 'ServicePriceType')


def checked_text(value):
    value = str(value)
    if any(not (c in '\t\n\r' or 0x20 <= ord(c) <= 0xD7FF
                or 0xE000 <= ord(c) <= 0xFFFD or 0x10000 <= ord(c) <= 0x10FFFF)
           for c in value):
        raise ValueError('В данных есть символы, недопустимые в XML.')
    return value


def xml_bytes(sheets):
    root = ET.Element('Ads', {'formatVersion': '3', 'target': 'Avito.ru'})
    ids, avito_ids = set(), set()
    for sheet in sheets:
        for row in sheet['rows']:
            item_id, avito_id = row.get('Id', ''), row.get('AvitoId', '')
            if (not item_id or len(item_id) > 100 or item_id in ids
                    or not re.fullmatch(r'[0-9]{1,20}', avito_id) or avito_id in avito_ids):
                raise ValueError('Для XML нужны уникальные Id и AvitoId существующих объявлений.')
            if row.get('Category') != 'Предложение услуг' or row.get('ListingFee') not in {'', 'Package'}:
                raise ValueError('XML поддерживает только услуги с размещением из пакета.')
            ids.add(item_id); avito_ids.add(avito_id)
            ad = ET.SubElement(root, 'Ad')
            for name, raw in row.items():
                if not raw or name in EXPORT_METADATA:
                    continue
                value = checked_text(raw)
                if name in SCALAR_FIELDS:
                    ET.SubElement(ad, name).text = value
                elif name in OPTION_FIELDS:
                    parent = ET.SubElement(ad, name)
                    for option in value.split('|'):
                        if not option.strip():
                            raise ValueError('Пустой вариант составного параметра XML.')
                        ET.SubElement(parent, 'Option').text = option.strip()
                elif name == 'ImageUrls':
                    from .avito_services_feed import images
                    parent = ET.SubElement(ad, 'Images')
                    for url in images(value).split(' | '):
                        ET.SubElement(parent, 'Image', {'url': url})
                elif name == 'PriceList':
                    lines = value.splitlines()
                    if not 1 <= len(lines) <= 50:
                        raise ValueError('В прайс-листе XML допускается от 1 до 50 услуг.')
                    parent = ET.SubElement(ad, 'PriceList')
                    for line in lines:
                        parts = [part.strip() for part in line.split('|')]
                        if (len(parts) != 5 or not parts[0] or not parts[4]
                                or not re.fullmatch(r'[0-9]+', parts[2])
                                or parts[3] not in {'', 'Да', 'Нет'}
                                or (parts[0] == 'Своя услуга' and not parts[1])):
                            raise ValueError('Некорректная строка прайс-листа XML.')
                        service = ET.SubElement(parent, 'Service')
                        for tag, text in zip(PRICE_FIELDS, parts):
                            if text:
                                ET.SubElement(service, tag).text = text
                else:
                    # Do not guess complex structures or forward paid promotion fields.
                    raise ValueError('В экспорте есть неподдерживаемый параметр XML. Нужен шаблон категории.')
            if not all(row.get(tag) for tag in ('Title', 'Description', 'ImageUrls')):
                raise ValueError('В XML отсутствует заголовок, описание или фотографии.')
    if not 1 <= len(ids) <= 200:
        raise ValueError('XML должен содержать от 1 до 200 объявлений.')
    ET.indent(root)
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)
