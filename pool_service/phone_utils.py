def normalize_phone(value):
    """Return a stable phone key for matching.

    Russian 10/11-digit numbers are normalized to 7XXXXXXXXXX.
    Other plausible international numbers keep their digits-only form.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits if 10 <= len(digits) <= 15 else ""


def format_phone(value):
    """Format Russian mobile/landline numbers as +7 XXX XXX XXXX."""
    normalized = normalize_phone(value)
    if len(normalized) == 11 and normalized.startswith("7"):
        return f"+7 {normalized[1:4]} {normalized[4:7]} {normalized[7:11]}"
    return str(value or "").strip()
