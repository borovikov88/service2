"""Interpret provider message text without exposing dynamic payloads or guessing codes."""
import re
from scripts.avito_message_diagnostics import make_diagnostics, project_diagnostics

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
    action = bool(re.search(r"не удалось|невозможно|ошибк|не найден|отклон|заблок|не заполн|исправ|укажите|добавьте|свяжите|необходимо|требуется|нужн|выберите|не\s+(?:был\w*\s+)?(?:привяз|связан|сопоставлен|выполнен)|не\s*был\w*\s+(?:привяз|связан|сопоставлен)|привязк\w*\s+(?:не|невозмож|отсутств)|нет\s+связ[ьи]", action_text))
    # Actual provider text may describe the link without the word "existing".
    # Future/conditional links are not proof of an already completed binding.
    uncertain_link = bool(re.search(r"\b(?:будет|будут|может|могут|если|чтобы|планируется|предполагается)\b", text)
                          or re.search(r"\b(?:будет|будут)связан", text))
    linked = bool(re.search(r"привязан|привязк|связан|связали|сопоставлен|сопоставили", text)) and not uncertain_link
    existing = bool(re.search(r"существующ|ранее|уже опубликован|связано\s+с\s+объявлением\s+на\s+авито", text))
    kind = message.get("type")
    category = ("action_required" if kind in {"error", "alarm"} or action else
        "existing_listing_link" if linked and existing else "information" if kind == "info" else "review_required")
    original = " ".join(str(message.get(key, "")) for key in ("title", "description"))
    fields = sorted(field for field in FIELDS if re.search(r"(?<![A-Za-z])" + field + r"(?![A-Za-z])", original))
    return {"category": category, "explanation": EXPLANATIONS[category], "fields": fields,
        "binding_evidence": bool(linked and existing and not action and kind not in {"error", "alarm"}),
        "action_evidence": action,
        "reason_keywords": sorted(word for word, pattern in REASONS.items() if re.search(pattern, text))}


def message_diagnostics(raw, normalized):
    """Called for a validated provider message, never for an ad row."""
    result = make_diagnostics(raw, normalized)
    raw = raw if isinstance(raw, dict) else {}
    normalized = normalized if isinstance(normalized, dict) else {}
    # Extract only fixed semantics locally before a potential blanket redaction.
    title = raw.get("title") if isinstance(raw.get("title"), str) and raw["title"] else normalized.get("title", "")
    description = raw.get("description") if isinstance(raw.get("description"), str) and raw["description"] else normalized.get("description", "")
    result["source_semantics"] = classify({"type": normalized.get("type"),
        "title": title[:50000] if isinstance(title, str) else "",
        "description": description[:50000] if isinstance(description, str) else ""})
    return result


def _semantic_evidence(message):
    fallback = classify(message)
    diagnostics = message.get("diagnostics")
    source = diagnostics.get("source_semantics") if isinstance(diagnostics, dict) else None
    if (not isinstance(source, dict) or not isinstance(source.get("category"), str) or source.get("category") not in EXPLANATIONS
            or source.get("explanation") != EXPLANATIONS[source["category"]]
            or type(source.get("binding_evidence")) is not bool or type(source.get("action_evidence")) is not bool
            or not isinstance(source.get("fields"), list) or not all(isinstance(field, str) and field in FIELDS for field in source["fields"])
            or not isinstance(source.get("reason_keywords"), list)
            or not all(isinstance(reason, str) and reason in REASONS for reason in source["reason_keywords"])):
        return fallback
    return {key: source[key] for key in ("category", "explanation", "fields",
            "binding_evidence", "action_evidence", "reason_keywords")}


def groups(rows):
    found = {}
    for row in rows:
        seen = set()
        for message in row.get("messages", []):
            key = (message["type"], message["code"])
            semantic = _semantic_evidence(message)
            group = found.setdefault(key, {"type": key[0], "code": key[1], "rows": 0, **semantic})
            group["_message_count"] = group.get("_message_count", 0) + 1
            diagnostics = message.get("diagnostics")
            if isinstance(diagnostics, dict):
                try:
                    sample = project_diagnostics(diagnostics)
                except ValueError:
                    sample = None
                if sample is not None:
                    group["_diagnostics_count"] = group.get("_diagnostics_count", 0) + 1
                    if "diagnostics" not in group:
                        group["diagnostics"] = sample
                        group["diagnostics_consistent"] = True
                    elif group["diagnostics"] != sample:
                        group["diagnostics_consistent"] = False
            if key not in seen:
                group["rows"] += 1
                seen.add(key)
            if PRIORITY[semantic["category"]] > PRIORITY[group["category"]]:
                group.update(category=semantic["category"], explanation=semantic["explanation"])
            group["fields"] = sorted(set(group["fields"]) | set(semantic["fields"]))
            group["reason_keywords"] = sorted(set(group["reason_keywords"]) | set(semantic["reason_keywords"]))
            group["binding_evidence"] = group["binding_evidence"] and semantic["binding_evidence"]
            group["action_evidence"] = group["action_evidence"] or semantic["action_evidence"]
    result = []
    for key in sorted(found):
        group = found[key]
        if "diagnostics" in group:
            group["diagnostics_consistent"] = bool(group["diagnostics_consistent"] and
                group.get("_diagnostics_count", 0) == group["_message_count"])
        result.append({name: value for name, value in group.items() if not name.startswith("_")})
    return result
