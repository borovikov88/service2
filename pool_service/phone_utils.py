from __future__ import annotations

import re


_RU_MATCH_RE = re.compile(r"^\d{10}$")


def normalize_phone(value: str | None) -> str:
    """Return a stable match key without guessing non-Russian numbers.

    Russian phones use the historical Service2 10-digit national key so
    authentication and existing phone lookups remain compatible. Explicit
    foreign international numbers keep their country code as +<digits>.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""

    digits = "".join(ch for ch in raw if ch.isdigit())
    if raw.startswith("+"):
        if len(digits) == 11 and digits.startswith("7"):
            return digits[1:]
        if 8 <= len(digits) <= 15:
            return "+" + digits
        return ""
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return digits[1:]
    if len(digits) == 10:
        return digits
    if 8 <= len(digits) <= 15:
        return digits
    return ""


def normalize_account_phone(value: str | None) -> str:
    """Return the strict 10-digit Russian key used by account usernames/SMS."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return digits[1:]
    if len(digits) == 10:
        return digits
    return ""


def format_phone(value: str | None) -> str:
    """Format Russian phones for UI; preserve foreign/unknown input."""
    raw = str(value or "").strip()
    normalized = normalize_phone(raw)
    if _RU_MATCH_RE.match(normalized):
        return f"+7 {normalized[:3]} {normalized[3:6]} {normalized[6:]}"
    return raw


def canonical_phone_value(value: str | None) -> str:
    """Canonical value suitable for persisted contact/display fields."""
    raw = str(value or "").strip()
    normalized = normalize_phone(raw)
    if _RU_MATCH_RE.match(normalized):
        return format_phone(normalized)
    return raw
