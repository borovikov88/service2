import re


def normalize_stable_identifier(value):
    if value is None:
        return None
    return re.sub(r"\s+", " ", str(value).strip()) or None


def normalize_source_identity_key(value):
    if value is None:
        return None
    return str(value).strip() or None
