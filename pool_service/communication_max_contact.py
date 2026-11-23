"""Treat a verified MAX phone as data belonging to the current bot dialog."""

import hmac
import re

from django.views.decorators.debug import sensitive_variables

from pool_service.phone_utils import normalize_phone


CONTACT_REQUEST_TEXT = "Если хотите, поделитесь номером телефона для уточнения этого обращения."
CONTACT_REQUEST_ATTACHMENT = {
    "type": "inline_keyboard",
    "payload": {"buttons": [[{"type": "request_contact", "text": "Поделиться телефоном"}]]},
}

TEL = re.compile(r"(?im)^(?:[a-z0-9_-]+\.)?TEL(?:;[^:\r\n]*)?:(?:tel:)?([+0-9 ().-]{8,32})[ \t]*$")
PHONE = re.compile(r"(?:[0-9]{10}|\+[1-9][0-9]{7,14})")
HASH = re.compile(r"[0-9a-fA-F]{64}")


@sensitive_variables("contact", "token", "card", "signature")
def shared_phone(contact, token, sender_id):
    if not token or not isinstance(contact, dict):
        return ""
    card, signature = contact.get("vcf_info"), contact.get("hash")
    profile = contact.get("max_info")
    if profile is not None and (
        not isinstance(profile, dict)
        or (profile.get("user_id") is not None and str(profile["user_id"]) != sender_id)
    ):
        return ""
    if (
        not isinstance(card, str) or not 1 <= len(card) <= 12000
        or not isinstance(signature, str) or not HASH.fullmatch(signature)
    ):
        return ""
    card = card.replace(r"\r\n", "\r\n")
    try:
        computed = hmac.digest(token.encode("utf-8"), card.encode("utf-8"), "sha256").hex()
    except UnicodeError:
        return ""
    if not hmac.compare_digest(computed, signature.lower()):
        return ""
    unfolded = re.sub(r"\r?\n[ \t]", "", card).replace("\r\n", "\n")
    candidates = {normalize_phone(value) for value in TEL.findall(unfolded)}
    candidates = {value for value in candidates if PHONE.fullmatch(value)}
    if len(candidates) != 1:
        return ""
    phone = candidates.pop()
    return phone if phone.startswith("+") else "+7" + phone
