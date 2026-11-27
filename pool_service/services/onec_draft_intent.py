"""Validate untrusted proposals before a private review and compile their typed 1C fields."""

import re
from decimal import Decimal


class InvalidIntent(ValueError):
    pass


def _fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() <= set(required) | set(optional):
        raise InvalidIntent("Проверьте состав полей запроса.")


def _text(value, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvalidIntent("Заполните текстовое поле в пределах указанной длины.")
    if any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF or ord(char) in (0xFFFE, 0xFFFF) for char in value):
        raise InvalidIntent("Поле должно содержать одну строку текста.")
    return value.strip()


def _number(value, *, zero_allowed=False):
    # JSON strings retain the quantity/price shown on the invoice exactly.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}([.,][0-9]{1,6})?", value):
        raise InvalidIntent("Укажите десятичное число без округления.")
    number = Decimal(value.replace(",", "."))
    if not zero_allowed and not number:
        raise InvalidIntent("Количество должно быть больше нуля.")
    normalized = format(number, "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _line(value):
    _fields(value, ("item", "quantity"), ("article", "unit", "price"))
    row = {"item": _text(value["item"]), "quantity": _number(value["quantity"])}
    for name in ("article", "unit"):
        if name in value:
            row[name] = _text(value[name], 100)
    if "price" in value:
        row["price"] = _number(value["price"], zero_allowed=True)
    return row


def document_intent(value):
    _fields(value, ("document_type",), ("organization", "counterparty", "source_number", "source_date", "lines", "details", "fields", "tables"))
    intent = {"kind": "document", "document_type": _text(value["document_type"], 100)}
    for name in ("organization", "counterparty", "source_number", "source_date", "details"):
        if name in value:
            intent[name] = _text(value[name], 500 if name == "details" else 160)
    lines = value.get("lines", [])
    if not isinstance(lines, list) or len(lines) > 100:
        raise InvalidIntent("В одном документе допускается до 100 строк.")
    if "fields" in value:
        intent["fields"] = _typed_fields(value["fields"])
    if "tables" in value:
        tables = value["tables"]
        if not isinstance(tables, dict) or len(tables) > 10:
            raise InvalidIntent("Допускается до десяти табличных частей.")
        output = {}
        for table, entries in tables.items():
            if not _identifier(table) or not isinstance(entries, list) or not 1 <= len(entries) <= 100:
                raise InvalidIntent("Проверьте название и строки табличной части.")
            output[table] = [_typed_fields(entry) for entry in entries]
        if sum(len(rows) for rows in output.values()) + len(lines) > 100:
            raise InvalidIntent("В одном документе допускается до 100 строк.")
        intent["tables"] = output
    if not (lines or intent.get("details") or intent.get("fields") or intent.get("tables")):
        raise InvalidIntent("Добавьте строки документа или описание операции.")
    intent["lines"] = [_line(row) for row in lines]
    return intent


def _identifier(name):
    return isinstance(name, str) and len(name) <= 100 and name.isidentifier() and not name.startswith("_")


def _typed_fields(fields):
    if not isinstance(fields, dict) or len(fields) > 80:
        raise InvalidIntent("Проверьте набор реквизитов 1С.")
    from .onec_write_documents import _protected
    result = {}
    for name, value in fields.items():
        if not _identifier(name) or _protected(name):
            raise InvalidIntent("Нельзя передавать системные реквизиты 1С.")
        if isinstance(value, str):
            result[name] = _text(value, 500)
        elif type(value) is bool or (type(value) is int and abs(value) < 10**18):
            result[name] = value
        else:
            raise InvalidIntent("Передайте числовое значение без округления или текст.")
    return result


def nomenclature_intent(value):
    if isinstance(value, dict) and "items" in value:
        _fields(value, ("items",))
        items = value["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 10:
            raise InvalidIntent("В одном запросе допускается от 1 до 10 карточек.")
        rows = [_card(item) for item in items]
        from .onec_write_connection import article_key
        keys = {article_key(item.get("article", "")) for item in rows}
        if "" in keys or len(keys) != len(rows):
            raise InvalidIntent("Для каждой позиции укажите отдельный артикул.")
        return {"kind": "nomenclature", "items": rows}
    return _card(value)


def _card(value):
    _fields(value, ("name", "unit"), ("article", "packaging", "group"))
    intent = {"kind": "nomenclature", "name": _text(value["name"]), "unit": _text(value["unit"], 100)}
    for name in ("article", "packaging", "group"):
        if name in value:
            intent[name] = _text(value[name])
    return intent
