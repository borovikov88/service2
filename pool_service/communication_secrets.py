import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class CommunicationSecretError(Exception):
    pass


def _fernet():
    key_source = getattr(settings, "COMMUNICATION_CREDENTIAL_KEY", "")
    if not key_source:
        key_source = settings.SECRET_KEY
    if not key_source:
        raise ImproperlyConfigured("COMMUNICATION_CREDENTIAL_KEY or SECRET_KEY is required")
    key = base64.urlsafe_b64encode(hashlib.sha256(key_source.encode("utf-8")).digest())
    return Fernet(key)


def encrypt_secret(value):
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_secret(value):
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise CommunicationSecretError("Communication credential cannot be decrypted") from exc
