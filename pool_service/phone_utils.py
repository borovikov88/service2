from __future__ import annotations


def normalize_phone(value) -> str:
    """Return a stable digits-only phone key.

    Russian 10-digit subscriber numbers and 11-digit numbers starting with
    7/8 are normalized to 7XXXXXXXXXX. Other plausible international numbers
    are kept digits-only so we do not silently rewrite them as Russian.
    """
    digits = "".join(character for character in str(value or "") if character.isdigit())
    if len(digits) == 10:
        digits = "7" + digits
    elif len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]
    return digits if 10 <= len(digits) <= 15 else ""


def format_phone(value) -> str:
    """Format Russian phones as +7 XXX XXX XXXX, preserving unknown formats."""
    raw = str(value or "").strip()
    normalized = normalize_phone(raw)
    if len(normalized) == 11 and normalized.startswith("7"):
        return f"+7 {normalized[1:4]} {normalized[4:7]} {normalized[7:11]}"
    return raw


def phone_variants(value) -> set[str]:
    """Common exact string variants for a normalized Russian phone."""
    normalized = normalize_phone(value)
    if not normalized:
        return set()
    variants = {normalized, str(value or "").strip()}
    if len(normalized) == 11 and normalized.startswith("7"):
        national = normalized[1:]
        variants.update(
            {
                "+" + normalized,
                "8" + national,
                national,
                format_phone(normalized),
            }
        )
    return {item for item in variants if item}
