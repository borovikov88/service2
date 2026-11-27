"""Compile approved document fields against the publishing employee's live 1C schema."""
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

from .onec_write_connection import IDENT, ITEMS, WriteError, guid, text_filter
from .onec_write_validation import checked_ref, require_profile, validate_links


ORDER = "Document_ЗаказПоставщику"
SYSTEM_FIELDS = {
    "ref_key", "number", "posted", "deletionmark", "dataversion", "linenumber",
    "проведен", "проведён", "пометкаудаления", "обменданными", "автор", "автор_key",
    "пользователь", "пользователь_key", "режимзаписи", "режимпроведения",
}


def type_name(hint):
    name = re.sub(r"\s+", "", hint)
    if name.startswith("Document_"):
        name = name[len("Document_"):]
    if not IDENT.fullmatch(name):
        raise WriteError("Укажите вид документа из метаданных 1С.")
    return "Document_" + name.casefold()


def entity_for(writer, hint):
    wanted = type_name(hint).casefold()
    matches = [name for name in writer.entities if name.startswith("Document_") and name.casefold() == wanted]
    if len(matches) != 1:
        raise WriteError("Этот вид документа не найден в опубликованной базе 1С.")
    writer.schema(matches[0], ("Ref_Key", "Posted", "Number", "Date", "DeletionMark"))
    return matches[0]


def _protected(field):
    return (not IDENT.fullmatch(field) or field.casefold() in SYSTEM_FIELDS or field.startswith("_")
            or field.casefold().startswith(("dataexchange", "обменданными", "additionalproperties")))


def scalar(field, dtype, value, writer):
    if _protected(field):
        raise WriteError("Системный реквизит документа нельзя задавать из чата.")
    if dtype == "Edm.Boolean" and type(value) is bool:
        return value
    if dtype == "Edm.Guid" and isinstance(value, str):
        return guid(value)
    if dtype in {"Edm.String", "Edm.DateTime", "Edm.DateTimeOffset", "Edm.Date"} and isinstance(value, str):
        if dtype in {"Edm.DateTime", "Edm.DateTimeOffset", "Edm.Date"}:
            try:
                parsed = date.fromisoformat(value) if dtype == "Edm.Date" else datetime.fromisoformat(value)
            except ValueError:
                raise WriteError("Дата реквизита должна быть в формате ISO.") from None
            return parsed.isoformat()
        if field.endswith("_Type") and (not value.startswith("StandardODATA.Catalog_")
                                       or value.partition(".")[2] not in writer.entities):
            raise WriteError("Для составной ссылки требуется опубликованный справочник.")
        return value
    if dtype in {"Edm.Int16", "Edm.Int32", "Edm.Int64", "Edm.Byte", "Edm.SByte"} and type(value) in (int, str):
        if re.fullmatch(r"-?[0-9]{1,18}", str(value)):
            return int(value)
    if dtype in {"Edm.Decimal", "Edm.Double", "Edm.Single"} and type(value) in (str, int):
        if re.fullmatch(r"-?[0-9]{1,16}(?:\.[0-9]{1,8})?", str(value)):
            return str(value) if dtype == "Edm.Decimal" else float(value)
    if dtype.startswith("StandardODATA.Enum_") and isinstance(value, str) and IDENT.fullmatch(value):
        return value
    raise WriteError(f"Значение реквизита «{field}» не соответствует его типу в 1С.")


def complex_fields(writer, declared):
    # Fresh's sanitized metadata has already passed the diagnostic XML/size checks.
    if not declared.startswith("Collection(") or not declared.endswith(")"):
        raise WriteError("Указанный реквизит не является табличной частью.")
    wanted = declared[len("Collection("):-1]
    ns, _, name = wanted.partition(".")
    node = writer.edm_nodes.get(writer.edm_aliases.get(ns, ns) + "." + name)
    if node is not None:
        return {p.get("Name"): p.get("Type") for p in node if p.tag.rsplit("}", 1)[-1] == "Property"}
    raise WriteError("Не удалось определить колонки табличной части в 1С.")


def _typed_row(writer, types, fields):
    if any(key not in types for key in fields):
        raise WriteError("Один из реквизитов отсутствует в метаданных 1С.")
    return {key: scalar(key, types[key], value, writer) for key, value in fields.items()}


def find_party(writer, entity, hint):
    if not hint:
        raise WriteError("Укажите организацию и контрагента по точному названию или ИНН/КПП.")
    if re.fullmatch(r"\d{10}(?:/\d{9})?|\d{12}", hint):
        inn, sep, kpp = hint.partition("/")
        where = text_filter("ИНН", inn)
        if sep:
            where += " and " + text_filter("КПП", kpp)
    else:
        where = text_filter("Description", hint)
    row = writer.active(entity, [], where)
    if not row:
        raise WriteError("Организация или контрагент не найдены под учётной записью сотрудника.")
    return guid(row["Ref_Key"])


def _scope_org(writer, key):
    allowed = {guid(ref) for ref in writer.config.organization_guids}
    if allowed and guid(key) not in allowed:
        raise WriteError("Организация вне настроенной области подключения 1С.")


def _friendly_order(writer, intent, supplier):
    rows = []
    for number, line in enumerate(intent["lines"], 1):
        if line.get("article"):
            card = writer.item(line["article"], supplier=supplier)
        else:
            candidates = []
            try:
                card = writer.active(ITEMS, ["ЕдиницаИзмерения_Key"],
                                     text_filter("Description", line["item"]) + " and IsFolder eq false", folder=False)
            except WriteError as exc:
                if "несколько совпадений" not in str(exc):
                    raise
                card = None
            if not card:
                candidates = writer.candidates_for(line["item"])
                writer.candidates[str(number)] = candidates
                selected = writer.choices.get(str(number))
                card = next((option for option in candidates if option["Ref_Key"] == selected), None)
        if not card:
            raise WriteError("Позиция заказа не найдена по точному артикулу или названию. Уточните товар. Для новой карточки нужно отдельное поручение.")
        unit = guid(card["ЕдиницаИзмерения_Key"])
        if line.get("unit"):
            explicit = writer.active("Catalog_КлассификаторЕдиницИзмерения", [], text_filter("Description", line["unit"]))
            if not explicit or guid(explicit["Ref_Key"]) != unit:
                raise WriteError("Единица из заказа отличается от единицы карточки. Уточните фасовку.")
        result = {"LineNumber": number, "Номенклатура_Key": guid(card["Ref_Key"]), "Количество": line["quantity"],
                  "ЕдиницаИзмерения": unit, "ЕдиницаИзмерения_Type": "StandardODATA.Catalog_КлассификаторЕдиницИзмерения"}
        if "price" in line:
            try:
                total = (Decimal(line["price"]) * Decimal(line["quantity"])).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            except InvalidOperation:
                raise WriteError("Проверьте суммы документа.") from None
            result.update({"Цена": line["price"], "Сумма": str(total), "Всего": str(total)})
        writer.reviewed_lines.append({"position": number, "requested": line["item"], "article": line.get("article", ""),
                                      "name": checked_ref(writer, ITEMS, card["Ref_Key"]), "quantity": line["quantity"],
                                      "unit": checked_ref(writer, "Catalog_КлассификаторЕдиницИзмерения", unit)})
        rows.append(result)
    if not rows:
        raise WriteError("Для заказа поставщику требуется хотя бы одна позиция.")
    return rows


def document_payload(writer, entity, intent, key, submitted_at):
    types = writer.schema(entity, ("Posted",)).field_types
    body = _typed_row(writer, types, intent.get("fields", {}))
    tables = intent.get("tables", {})
    if set(tables) & body.keys():
        raise WriteError("Табличная часть указана повторно.")
    for name, rows in tables.items():
        if _protected(name) or name not in types:
            raise WriteError("Указанная табличная часть недоступна.")
        columns = complex_fields(writer, types[name])
        body[name] = []
        for index, row in enumerate(rows, 1):
            out = _typed_row(writer, columns, row)
            if "LineNumber" in columns:
                out["LineNumber"] = index
            body[name].append(out)
    dedicated = {}
    if intent.get("organization"):
        dedicated["Организация_Key"] = find_party(writer, "Catalog_Организации", intent["organization"])
    if intent.get("counterparty"):
        dedicated["Контрагент_Key"] = find_party(writer, "Catalog_Контрагенты", intent["counterparty"])
    if intent.get("source_number"):
        dedicated["НомерВходящегоДокумента"] = intent["source_number"]
    if intent.get("source_date"):
        try:
            dedicated["ДатаВходящегоДокумента"] = date.fromisoformat(intent["source_date"]).isoformat() + "T00:00:00"
        except ValueError:
            raise WriteError("Дата счёта должна быть указана в формате ГГГГ-ММ-ДД.") from None
    if intent.get("details"):
        dedicated["Комментарий"] = intent["details"]
    if intent.get("lines"):
        if entity != ORDER:
            raise WriteError("Для этого вида документа укажите табличные части по опубликованной схеме.")
        dedicated["Запасы"] = _friendly_order(writer, intent, dedicated.get("Контрагент_Key") or body.get("Контрагент_Key"))
        columns = complex_fields(writer, types.get("Запасы", ""))
        if any(not row.keys() <= columns.keys() for row in dedicated["Запасы"]):
            raise WriteError("Колонки заказа отличаются от ожидаемых. Используйте табличные части по схеме 1С.")
        if any("Цена" in row for row in dedicated["Запасы"]):
            if any("Цена" not in row for row in dedicated["Запасы"]):
                raise WriteError("Укажите цену каждой позиции заказа. Для бесплатной позиции укажите 0.")
            dedicated["СуммаДокумента"] = str(sum(Decimal(row["Всего"]) for row in dedicated["Запасы"]))
    if body.keys() & dedicated.keys():
        raise WriteError("Один реквизит указан дважды. Оставьте один вариант.")
    body.update(dedicated)
    if "Организация_Key" in types:
        if not body.get("Организация_Key"):
            raise WriteError("Выберите организацию документа.")
        _scope_org(writer, body["Организация_Key"])
    if body.get("НомерВходящегоДокумента") and not body.get("ДатаВходящегоДокумента"):
        raise WriteError("Для проверки повторного счёта укажите его дату вместе с номером.")
    if entity == ORDER and (not body.get("Контрагент_Key") or not body.get("Запасы")):
        raise WriteError("Для заказа укажите поставщика и позиции.")
    require_profile(entity, body)
    validate_links(writer, writer.entities[entity].entity_type, types, {name: val for name, val in body.items() if not isinstance(val, list)})
    for name, rows in body.items():
        if isinstance(rows, list):
            declared = types[name]
            columns = complex_fields(writer, declared)
            for row in rows:
                validate_links(writer, declared[len("Collection("):-1], columns, row)
    if "Date" not in body:
        try:
            accounting_zone = ZoneInfo(settings.ONEC_FINANCE_TIME_ZONE)
        except (ValueError, ZoneInfoNotFoundError):
            raise WriteError("Проверьте настройку часового пояса учёта 1С.") from None
        local_time = timezone.localtime(submitted_at, accounting_zone)
        body["Date"] = local_time.replace(microsecond=0, tzinfo=None).isoformat()
    body.update({"Ref_Key": key, "Posted": False})
    writer.schema(entity, body)
    return body
