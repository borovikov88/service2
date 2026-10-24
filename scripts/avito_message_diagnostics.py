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
SYSTEM_WORDS.update("""аккаунт аккаунта аккаунте аккаунтов кабинет кабинета кабинете
тариф тарифа тарифе тарифов тарифный тарифного подписка подписки подписке
продление продления продлить повторно повторная повторной переопубликовать
снято снят сняты код кода коду формат формата формате версия версии изменение
действует действуют доступ доступа доступен доступна лимит лимита лимиты лимитов
стоимость стоимости бесплатно бесплатное бесплатная оплата оплаты оплате оплачено""".split())
MISSING = object()

SKELETON_PLACEHOLDER = "[скрыто]"
SKELETON_MAX_INPUT = 2000
SKELETON_MAX_LENGTH = 200
SKELETON_POLICIES = {"safe", "redacted", "all_withheld", "empty", "non_text",
                     "too_long", "sensitive_marker", "not_system_message"}
_SENSITIVE_MARKERS = re.compile(
    r"access[\s_]*token|client[\s_]*(?:secret|id)|refresh[\s_]*token|password|authorization|bearer|secret|парол|токен",
    re.I)
_HIDDEN = "\x00"


def system_title_skeleton(value):
    """Retain only canonical vocabulary; quoted/contact/value spans never survive."""
    if not isinstance(value, str):
        return None, "non_text"
    if len(value) > SKELETON_MAX_INPUT:
        return None, "too_long"
    if _SENSITIVE_MARKERS.search(value):
        return None, "sensitive_marker"
    if not value.strip():
        return None, "empty"
    # Hide a whole quoted span even if its user value consists of allowed words.
    # An unmatched opening quote hides the rest instead of guessing the boundary.
    pairs = {'"': '"', "'": "'", chr(96): chr(96), "«": "»", "“": "”",
             "„": "“", "‘": "’", "‹": "›", "<": ">", "[": "]", "{": "}"}
    quoted, position = [], 0
    while position < len(value):
        char = value[position]
        if char in pairs:
            end = value.find(pairs[char], position + 1)
            interior = value[position + 1:end] if end >= 0 else ""
            # Nested/mixed openers or escape syntax make the boundary ambiguous.
            # Hide the remainder instead of exposing a tail inside a user value.
            ambiguous = "\\" in interior or any(opener in interior for opener in pairs)
            quoted.append(" " + _HIDDEN + " ")
            position = len(value) if end < 0 or ambiguous else end + 1
        else:
            quoted.append(char)
            position += 1
    text = "".join(quoted)
    # Replace complete URLs, emails and paths before tokenisation. Parts such as
    # "error" or "id" in a private address cannot turn into system evidence.
    text = re.sub(r"(?:[A-Za-z][A-Za-z0-9+.-]*://|www\.)[^\s<>\"'«»“”\x60]+",
                  " " + _HIDDEN + " ", text, flags=re.I)
    text = re.sub(r"[^\s<>\"'«»“”\x60]*@[^\s<>\"'«»“”\x60]*",
                  " " + _HIDDEN + " ", text)
    text = re.sub(r"\b(?:[A-Za-zА-Яа-яЁё0-9-]+\.)+[A-Za-zА-Яа-яЁё]{2,}(?::[0-9]+)?",
                  " " + _HIDDEN + " ", text)
    text = re.sub(r"[^\s<>\"'«»“”\x60]*[/\\][^\s<>\"'«»“”\x60]*",
                  " " + _HIDDEN + " ", text)
    # A recognised word inside an identifier/value is not a separate system
    # token. Hide complete compound lexemes and explicit assignment values.
    text = re.sub(r"(\w+)\s*=\s*[^\s,;]+", lambda match: match.group(1) + " " + _HIDDEN, text)
    text = re.sub(r"\b(id|avitoid)\s*[:#]\s*[^\s,;]+",
                  lambda match: match.group(1) + " " + _HIDDEN, text, flags=re.I)
    text = re.sub(r"(?<!\w)\w+(?:[-_]\w+)+(?!\w)", " " + _HIDDEN + " ", text)
    tokens, redacted = [], False
    for token in re.findall(r"\w+|\x00|[^\w\s]", text, flags=re.UNICODE):
        canonical = token.casefold()
        if canonical in SYSTEM_WORDS and token.isalpha():
            output = canonical
        elif token in ".,:;!?()—–-":
            continue
        else:
            output, redacted = SKELETON_PLACEHOLDER, True
        if output != SKELETON_PLACEHOLDER or not tokens or tokens[-1] != output:
            tokens.append(output)
    if not any(token != SKELETON_PLACEHOLDER for token in tokens):
        return None, "all_withheld"
    result = " ".join(tokens)
    if len(result) > SKELETON_MAX_LENGTH:
        return None, "too_long"
    return result, "redacted" if redacted else "safe"



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
    if _SENSITIVE_MARKERS.search(text):
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
    skeleton, skeleton_policy = system_title_skeleton(raw.get("title")) if shape_valid else (None, "not_system_message")
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
        "system_title": title, "system_title_policy": policy,
        "system_title_redacted": skeleton, "system_title_redacted_policy": skeleton_policy}


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
    # Older cached diagnostics omit these fields; no skeleton is inferred from
    # a normalised title, an ad row or a provider description.
    if "system_title_redacted" in value or "system_title_redacted_policy" in value:
        skeleton, skeleton_policy = value.get("system_title_redacted"), value.get("system_title_redacted_policy")
        require(isinstance(skeleton_policy, str) and skeleton_policy in SKELETON_POLICIES)
        if policy == "sensitive_marker":
            require(skeleton is None and skeleton_policy == "sensitive_marker")
        if skeleton is not None:
            require(isinstance(skeleton, str) and 0 < len(skeleton) <= SKELETON_MAX_LENGTH
                    and skeleton == " ".join(skeleton.split()) and result["provider_message_shape_valid"]
                    and result["raw_title"]["type"] == "string" and result["raw_title"]["length"] > 0)
            parts = skeleton.split(" ")
            require(all(part in SYSTEM_WORDS or part == SKELETON_PLACEHOLDER for part in parts))
            require(any(part != SKELETON_PLACEHOLDER for part in parts))
            require(not any(left == right == SKELETON_PLACEHOLDER for left, right in zip(parts, parts[1:])))
            require(skeleton_policy == ("redacted" if SKELETON_PLACEHOLDER in parts else "safe"))
        else:
            require(skeleton_policy not in {"safe", "redacted"})
        result.update(system_title_redacted=skeleton, system_title_redacted_policy=skeleton_policy)
    return result
