"""Business requirements not carried by EDM, and live validation of 1C references."""
import json
import os
from decimal import Decimal, InvalidOperation

from django.conf import settings

from .onec_write_connection import IDENT, ITEMS, UNITS, WriteError, guid, reference_filter


ORDER = "Document_ЗаказПоставщику"
TRANSFER = "Document_ПеремещениеЗапасов"
STOCK_ROWS = {"Запасы": ["Номенклатура_Key", "Количество"]}
PROFILES = {
    ORDER: {"fields": ["Организация_Key", "Контрагент_Key"], "tables": STOCK_ROWS},
    TRANSFER: {"fields": ["Организация_Key", "СтруктурнаяЕдиница_Key", "СтруктурнаяЕдиницаПолучатель_Key"],
               "tables": STOCK_ROWS},
}
KNOWN_REFERENCES = {
    "Организация_Key": "Catalog_Организации", "Контрагент_Key": "Catalog_Контрагенты",
    "Номенклатура_Key": ITEMS, "СтруктурнаяЕдиница_Key": "Catalog_СтруктурныеЕдиницы",
    "СтруктурнаяЕдиницаПолучатель_Key": "Catalog_СтруктурныеЕдиницы", "ЕдиницаИзмерения_Key": UNITS,
}


def require_profile(entity, body):
    if entity in PROFILES:
        profile = PROFILES[entity]
    else:
        try:
            extra = getattr(settings, "ONEC_MCP_DOCUMENT_PROFILES", None)
            if extra is None:
                extra = json.loads(os.environ.get("ONEC_MCP_DOCUMENT_PROFILES_JSON", "{}"))
            profile = extra[entity]
        except (ValueError, TypeError, KeyError):
            raise WriteError("Для этого документа ещё не настроена проверка обязательных реквизитов 1С.") from None
    if not isinstance(profile, dict) or not isinstance(profile.get("fields"), list) or not isinstance(profile.get("tables"), dict):
        raise WriteError("Проверьте профиль обязательных реквизитов этого документа.")
    names, tables = profile["fields"], profile["tables"]
    if not (names or tables) or not all(isinstance(f, str) and IDENT.fullmatch(f) for f in names):
        raise WriteError("Нужен действующий профиль обязательных реквизитов документа.")
    for name in names:
        if not body.get(name):
            raise WriteError(f"Заполните обязательный реквизит «{name}».")
    for name, columns in tables.items():
        rows = body.get(name)
        if (not isinstance(columns, list) or not columns
                or not all(isinstance(f, str) and IDENT.fullmatch(f) for f in columns)):
            raise WriteError("Проверьте профиль колонок табличной части.")
        if not isinstance(rows, list) or not rows:
            raise WriteError(f"Заполните табличную часть «{name}».")
        for row in rows:
            if any(row.get(column) is None or row.get(column) == "" for column in columns):
                raise WriteError(f"Заполните обязательные колонки табличной части «{name}».")
            if "Количество" in columns:
                try:
                    if Decimal(str(row["Количество"])) <= 0:
                        raise InvalidOperation
                except (InvalidOperation, ValueError):
                    raise WriteError("Количество в документе должно быть больше нуля.") from None
    if entity == TRANSFER and body.get("СтруктурнаяЕдиница_Key") == body.get("СтруктурнаяЕдиницаПолучатель_Key"):
        raise WriteError("Для перемещения нужны разные склады отправления и получения.")


def _qualified(writer, value):
    ns, _, name = value.partition(".")
    return writer.edm_aliases.get(ns, ns) + "." + name


def _link_target(writer, owner_type, field):
    # EDM v3 associations on the declared table-row or header type override UNF fallbacks.
    navigation = writer.edm_navigations.get((owner_type, field.removesuffix("_Key")))
    if navigation:
        declared = writer.edm_ends.get((_qualified(writer, navigation[0]), navigation[1]))
        if declared:
            entities = [entity for entity, schema in writer.entities.items() if schema.entity_type == _qualified(writer, declared)]
            if len(entities) == 1:
                return entities[0]
    return KNOWN_REFERENCES.get(field)


def checked_ref(writer, entity, key):
    key = guid(key)
    if entity == "Catalog_Организации" and key not in {guid(x) for x in writer.config.organization_guids}:
        raise WriteError("Организация вне области этого подключения 1С.")
    existing = writer.resolved.get((entity, key))
    if existing:
        return existing
    schema = writer.entities.get(entity)
    if schema is None or not entity.startswith(("Catalog_", "Document_")):
        raise WriteError("Нельзя проверить вид связанного объекта по опубликованной схеме.")
    fields = schema.field_types
    label = "Description" if "Description" in fields else "Number"
    row = writer.active(entity, [label], reference_filter("Ref_Key", key), folder=False if "IsFolder" in fields else None)
    if not row:
        raise WriteError("Связанный объект отсутствует или недоступен сотруднику в 1С.")
    writer.resolved[(entity, key)] = str(row[label])
    return writer.resolved[(entity, key)]


def validate_links(writer, owner_type, field_types, fields):
    for field, value in fields.items():
        if field.endswith("_Type"):
            name = field[:-5]
            if not isinstance(value, str) or not value.startswith("StandardODATA.Catalog_") or not fields.get(name):
                raise WriteError("У составной ссылки укажите вид и значение из 1С.")
            checked_ref(writer, value.partition(".")[2], fields[name])
        elif field_types.get(field) == "Edm.Guid" or field.endswith("_Key"):
            target = _link_target(writer, owner_type, field)
            if not target:
                raise WriteError(f"Нельзя однозначно проверить ссылку «{field}» в 1С.")
            checked_ref(writer, target, value)
        elif field + "_Type" in field_types and field + "_Type" not in fields:
            raise WriteError(f"Укажите тип ссылки «{field}» из 1С.")
