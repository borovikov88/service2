"""Pure allowlisted provider SYSTEM-message diagnostics; no Django or provider requests."""
import re

ALTERNATE_TEXT_KEYS = ("text", "message", "content", "value", "details", "description_html")
VALUE_TYPES = {"missing", "null", "string", "object", "array", "boolean", "number", "other"}
MAX_LENGTH = 50000
REDACTION_MARKERS = {"[текст с учётными данными скрыт]", "[контакт скрыт]", "[телефон скрыт]", "[токен скрыт]"}
TITLE_POLICIES = {"empty", "non_text", "too_long", "sensitive_marker", "contact_or_value", "unknown_words", "not_system_message", "safe"}
SYSTEM_WORDS = set("""объявление объявления объявлений объявлению объявлением объявлении
существующее существующего существующие существующих существующему существующим
новое нового новые новых ранее уже опубликовано опубликованное опубликованного
опубликованные опубликованных публикация публикации опубликован опубликованы
привязка привязки привязке привязку привязано привязаны привязан привязанный привязанных
связь связи связано связаны связан связанного связанных сопоставление сопоставления
сопоставлено сопоставлен сопоставлены сопоставленных идентификатор идентификатора
идентификаторы идентификаторов внешний внешние внешнего внешних внутренний внутренние
авито автозагрузка автозагрузки автозагрузке загрузка загрузки загрузке файл файла файле файлов
источник источника источнике обработка обработки проверка проверки проверке результат результаты
успешно выполнена выполнено завершена завершено информация информационное сообщение
предупреждение предупреждения ошибка ошибки ошибкой ошибок параметры параметр параметра параметров
поле поля полей значение значения значений обязательное обязательные обязательного
неверное неверный неверные неверного некорректное некорректный некорректные отсутствует отсутствуют
описание описания заголовок заголовка фотографии фотографий фотография изображение изображения
изображений цена цены прайс лист список адрес адреса город города категория категории
категорию услуга услуги услуг телефон телефона номер номера контакт контакты контактов
доставка доставки документы документов данные данных документ документа срок срока
размещение размещения дата даты окончание окончания истек истёк истекло истёкло сроков
активно активное активных архив архиве отклонено отклонен заблокировано
дубликат дубликаты дублирование дублируется найдено найден найдены найденное требуется требуются
необходимо нужно укажите выберите добавьте исправьте заполните поддерживается поддерживаются
недоступно недоступен требуют нет ничего дополнительных действия действий
изменения изменений исправления исправлений делать менять исправлять заполнено заполнены
в во на с со к ко от для из и или по без не до после при о об обо это этой этого тот все всех
проблема проблемы проблемой описанием описании заголовком заголовке фотографиями
возможна возможно невозможно невозможна недоступна поддержка поддержки результата результатов
avito autoload warning error information info message existing listing file source id avitoid
title description category servicetype servicesubtype address contactphone images imageurls
price pricelist dateend tnved xml тн вэд api""".split())
MISSING = object()


def shape(value=MISSING):
    if value is MISSING:
        return {"present": False, "type": "missing", "length": None, "capped": False}
    kind = ("null" if value is None else "boolean" if type(value) is bool else
            "number" if type(value) in (int, float) else "string" if isinstance(value, str) else
            "object" if isinstance(value, dict) else "array" if isinstance(value, list) else "other")
    size = len(value) if isinstance(value, (str, dict, list)) else None
    return {"present": True, "type": kind, "length": min(size, MAX_LENGTH) if size is not None else None,
            "capped": size is not None and size > MAX_LENGTH}


def system_title(value):
    if not isinstance(value, str):
        return None, "non_text"
    text = " ".join(value.split())
    if not text:
        return None, "empty"
    if len(text) > 200:
        return None, "too_long"
    if re.search(r"access[\s_]*token|client[\s_]*(?:secret|id)|refresh[\s_]*token|password|authorization|bearer|secret|парол|токен", text, re.I):
        return None, "sensitive_marker"
    if (any(char.isdigit() for char in text) or
            re.search(r"""[<>"'«»“”\x60@/\\]|https?|www\.""", text, re.I) or
            re.search(r"[^A-Za-zА-Яа-яЁё\s.,:;!?()—–-]", text)):
        return None, "contact_or_value"
    words = re.findall(r"[A-Za-zА-Яа-яЁё]+", text.casefold())
    if not words or any(word not in SYSTEM_WORDS for word in words):
        return None, "unknown_words"
    return text, "safe"


def make_diagnostics(raw, normalized):
    raw = raw if isinstance(raw, dict) else {}
    normalized = normalized if isinstance(normalized, dict) else {}
    shape_valid = raw.get("type") in {"error", "warning", "alarm", "info"} and type(raw.get("code")) is int
    title, policy = system_title(raw.get("title")) if shape_valid else (None, "not_system_message")
    norm_title = normalized.get("title") if isinstance(normalized.get("title"), str) else ""
    norm_description = normalized.get("description") if isinstance(normalized.get("description"), str) else ""
    raw_title, raw_description = raw.get("title"), raw.get("description")
    return {"source": "provider_system_message", "provider_message_shape_valid": shape_valid,
        "raw_title": shape(raw.get("title", MISSING)), "raw_description": shape(raw.get("description", MISSING)),
        "normalized_title_length": min(len(norm_title), MAX_LENGTH),
        "normalized_description_length": min(len(norm_description), MAX_LENGTH),
        "normalized_title_fully_redacted": norm_title.strip().casefold() in REDACTION_MARKERS,
        "normalized_description_fully_redacted": norm_description.strip().casefold() in REDACTION_MARKERS,
        "title_entirely_withheld": bool(isinstance(raw_title, str) and raw_title.strip() and title is None),
        "description_entirely_withheld": bool(norm_description.strip() or isinstance(raw_description, (str, dict, list)) and raw_description),
        "alternate_text_keys": {key: key in raw for key in ALTERNATE_TEXT_KEYS},
        "system_title": title, "system_title_policy": policy}


def project_diagnostics(value):
    """Drop unrecognised payload fields and reject invalid or unsafe title proof."""
    def require(condition):
        if not condition:
            raise ValueError("invalid message diagnostics")
    require(isinstance(value, dict) and value.get("source") == "provider_system_message")
    require(type(value.get("provider_message_shape_valid")) is bool)
    result = {"source": "provider_system_message", "provider_message_shape_valid": value["provider_message_shape_valid"]}
    for key in ("raw_title", "raw_description"):
        raw = value.get(key)
        require(isinstance(raw, dict) and type(raw.get("present")) is bool and
                raw.get("type") in VALUE_TYPES and type(raw.get("capped")) is bool)
        size = raw.get("length")
        require(size is None or type(size) is int and 0 <= size <= MAX_LENGTH)
        require(raw["present"] == (raw["type"] != "missing"))
        require((raw["type"] in {"string", "object", "array"}) == (size is not None))
        require(not raw["capped"] or size == MAX_LENGTH)
        result[key] = {name: raw[name] for name in ("present", "type", "length", "capped")}
    for key in ("normalized_title_length", "normalized_description_length"):
        size = value.get(key)
        require(type(size) is int and 0 <= size <= MAX_LENGTH)
        result[key] = size
    for key in ("normalized_title_fully_redacted", "normalized_description_fully_redacted",
                "title_entirely_withheld", "description_entirely_withheld"):
        require(type(value.get(key)) is bool)
        result[key] = value[key]
    alternate = value.get("alternate_text_keys")
    require(isinstance(alternate, dict) and set(alternate) == set(ALTERNATE_TEXT_KEYS) and
            all(type(alternate[key]) is bool for key in ALTERNATE_TEXT_KEYS))
    result["alternate_text_keys"] = {key: alternate[key] for key in ALTERNATE_TEXT_KEYS}
    title, policy = value.get("system_title"), value.get("system_title_policy")
    require(policy in TITLE_POLICIES)
    if title is not None:
        safe, checked = system_title(title)
        require(checked == "safe" and safe == title and policy == "safe" and result["provider_message_shape_valid"] and
                result["raw_title"]["type"] == "string" and result["raw_title"]["length"] > 0
                and result["title_entirely_withheld"] is False)
    else:
        require(policy != "safe")
    result.update(system_title=title, system_title_policy=policy)
    return result
