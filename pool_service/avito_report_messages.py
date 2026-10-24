"""Interpret provider message text without exposing dynamic payloads or guessing codes."""
import re

EXPLANATIONS = {
    "existing_listing_link": "Сообщение о привязке существующего объявления; само по себе исправления не требует.",
    "information": "Информационное сообщение Авито.",
    "action_required": "Авито сообщает о проблеме, требующей проверки и исправления.",
    "review_required": "Предупреждение требует просмотра; по одному коду или количеству причина не определяется.",
}
FIELDS = {"Id", "AvitoId", "Title", "Description", "Category", "ServiceType", "ServiceSubtype",
          "Address", "ContactPhone", "Images", "ImageUrls", "Price", "PriceList", "DateEnd", "TNVED"}
REASONS = {"номер телефона": r"номер.*телефон|телефон", "ТН ВЭД": r"тн\s*вэд|tnved",
    "фотографии": r"фотограф|изображен", "срок размещения": r"срок|дат.*окончан",
    "не поддерживается": r"не поддержива", "категория": r"категори",
    "обязательное поле": r"обязательн.*пол|не заполн", "контакты в описании": r"контакт.*описани",
    "существующее объявление": r"существующ.*объявлен"}
PRIORITY = {"information": 0, "existing_listing_link": 1, "review_required": 2, "action_required": 3}


def classify(message):
    text = " ".join(str(message.get(key, "")) for key in ("title", "description")).casefold()
    # Remove only an explicit no-action phrase, not the rest of a mixed message.
    action_text = re.sub(r"ничего\s+(?:исправлять|менять|добавлять|делать)\s+не\s+(?:нужно|требуется)|"
        r"(?:дополнительных\s+)?(?:исправлений|действий|изменений)\s+не\s+требуется|"
        r"не\s+(?:нужно|требуется)\s+(?:ничего\s+)?(?:исправлять|менять|добавлять|делать)|"
        r"ошибок\s+нет|без\s+ошибок", "", text)
    action = bool(re.search(r"не удалось|невозможно|ошибк|не найден|отклон|заблок|не заполн|исправ|укажите|добавьте|свяжите|необходимо|требуется|нужн|выберите|не\s+(?:был\w*\s+)?(?:привязан|связан|сопоставлен)", action_text))
    linked = bool(re.search(r"привязан|связан|связали|сопоставлен|сопоставили", text))
    existing = bool(re.search(r"существующ|ранее|уже опубликован", text))
    kind = message.get("type")
    category = ("action_required" if kind in {"error", "alarm"} or action else
        "existing_listing_link" if linked and existing else "information" if kind == "info" else "review_required")
    original = " ".join(str(message.get(key, "")) for key in ("title", "description"))
    fields = sorted(field for field in FIELDS if re.search(r"(?<![A-Za-z])" + field + r"(?![A-Za-z])", original))
    return {"category": category, "explanation": EXPLANATIONS[category], "fields": fields,
        "binding_evidence": bool(linked and existing and not action),
        "action_evidence": action,
        "reason_keywords": sorted(word for word, pattern in REASONS.items() if re.search(pattern, text))}


def groups(rows):
    found = {}
    for row in rows:
        seen = set()
        for message in row.get("messages", []):
            key = (message["type"], message["code"])
            semantic = classify(message)
            group = found.setdefault(key, {"type": key[0], "code": key[1], "rows": 0, **semantic})
            if key not in seen:
                group["rows"] += 1
                seen.add(key)
            if PRIORITY[semantic["category"]] > PRIORITY[group["category"]]:
                group.update(category=semantic["category"], explanation=semantic["explanation"])
            group["fields"] = sorted(set(group["fields"]) | set(semantic["fields"]))
            group["reason_keywords"] = sorted(set(group["reason_keywords"]) | set(semantic["reason_keywords"]))
            group["binding_evidence"] = group["binding_evidence"] and semantic["binding_evidence"]
            group["action_evidence"] = group["action_evidence"] or semantic["action_evidence"]
    return [found[key] for key in sorted(found)]
