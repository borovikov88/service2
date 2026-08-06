import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test-only-not-for-production")

from .settings import *  # noqa: F403,F401

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
DEBUG = True
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
EMAIL_HOST = ""
EMAIL_HOST_USER = ""
EMAIL_HOST_PASSWORD = ""
SMS_RU_API_ID = ""
VAPID_PRIVATE_KEY = ""
VAPID_PUBLIC_KEY = ""
YANDEX_SUGGEST_API_KEY = ""
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
_TEST_PRIVATE_MEDIA = tempfile.TemporaryDirectory(prefix="service2-test-private-")
_TEST_PUBLIC_MEDIA = tempfile.TemporaryDirectory(prefix="service2-test-media-")
PRIVATE_MEDIA_ROOT = _TEST_PRIVATE_MEDIA.name
MEDIA_ROOT = _TEST_PUBLIC_MEDIA.name
