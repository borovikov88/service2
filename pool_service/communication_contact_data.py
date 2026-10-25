"""Bounded, deterministic extraction; messages are data, never instructions."""
import re
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from .phone_utils import canonical_phone_value, normalize_phone


def text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def avito_url(value, *, image=False):
    value = text(value, 2048)
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        roots = ("avito.ru", "avito.st") if image else ("avito.ru",)
        allowed = any(host == root or host.endswith("." + root) for root in roots)
        if parsed.scheme == "https" and allowed and not parsed.username and not parsed.password and parsed.port in (None, 443):
            return value
    except ValueError:
        pass
    return ""


def clean_listing(value):
    if not isinstance(value, dict):
        return {}
    result = {key: text(value.get(key), limit) for key, limit in
              (("id", 80), ("title", 255), ("price", 80), ("location", 255))}
    result["url"] = avito_url(value.get("url"))
    result["image_url"] = avito_url(value.get("image_url"), image=True)
    return result if result["title"] or result["id"] else {}


def extract_contacts(body):
    body = text(body, 10000)
    phones, emails = [], []
    phone_body = re.sub(r"(?:https?://|www\.)\S+|\S+@\S+", " ", body, flags=re.I)
    for match in re.finditer(r"(?<![\w])\+?\d[\d ()\-]{7,25}\d(?![\w])", phone_body):
        raw = match.group().strip()
        digits = re.sub(r"\D", "", raw)
        prefix = phone_body[max(0, match.start() - 36):match.start()].lower()
        if re.search(r"(?:инн|огрн|заказ[а-я]*|договор[а-я]*|объявлени[а-я]*|цена|бюджет|стоимость)\s*[:№#]?\s*$", prefix):
            continue
        if not raw.startswith("+") and not (
            len(digits) == 11 and digits[0] in "78"
            or len(digits) == 10 and digits[0] == "9"
        ):
            continue
        key = normalize_phone(raw)
        if key and key not in phones:
            phones.append(key)
    for raw in re.findall(r"[\w.!#$%&'*+/=?^`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", body):
        value = raw.strip(".,;:").casefold()
        try:
            validate_email(value)
        except ValidationError:
            continue
        if len(value) <= 254 and value not in emails:
            emails.append(value)
    result = {"phones": phones[:10], "emails": emails[:10]}
    name = re.search(r"(?:меня\s+зовут|мо[её]\s+имя\s*[:—-]?|(?:^|\n)\s*(?:имя|фио)\s*:)\s*([^\n,.;:!?\d]{2,100})", body, re.I)
    if name:
        value = re.split(r"\s+(?:я|мой|моя|телефон|номер|хочу|нужен|нужна|и)\b", name.group(1), maxsplit=1, flags=re.I)[0].strip()
        words = value.split()
        if 1 <= len(words) <= 4 and all(re.fullmatch(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", word) for word in words):
            result["name"] = value.title() if value.islower() else value
    for key, label, limit in (("address", r"(?:мой\s+)?адрес", 500), ("company", r"(?:моя\s+)?компания", 255)):
        found = re.search(r"(?:^|\n)\s*" + label + r"\s*[:—-]\s*([^\n]{2,500})", body, re.I)
        if found:
            result[key] = found.group(1).strip()[:limit]
    return result


def phone_value(key):
    return canonical_phone_value(key)
