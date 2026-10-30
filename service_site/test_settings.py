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
# In-memory SQLite makes Django LiveServer share one DatabaseWrapper among
# HTTP threads. Statement caches AND atomic session writes are unsafe there.
# An explicit private file gives each thread its normal independent connection.
SQLITE_LIVE_SERVER_TEST = any(os.environ.get(flag) == "1" for flag in (
    "CRM_CARD_BROWSER_TEST", "CRM_NEEDS_BROWSER_TEST", "CRM_LEADS_BROWSER_TEST",
    "CONTACTS_BROWSER_TEST", "CRM_NON_SALES_BROWSER_TEST",
    "SERVICE2_SQLITE_LIVE_SERVER_TEST",
))
if SQLITE_LIVE_SERVER_TEST:
    _TEST_DATABASE_DIR = tempfile.TemporaryDirectory(prefix="service2-test-db-")
    DATABASES["default"]["TEST"] = {
        "NAME": os.path.join(_TEST_DATABASE_DIR.name, "test.sqlite3"),
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
OPENAI_API_KEY = ""
OPENAI_DEVELOPMENT_MODEL = "test-model"
GITHUB_DEVELOPMENT_TOKEN = ""
GITHUB_DEVELOPMENT_REPOSITORY = "borovikov88/service2"
GITHUB_DEVELOPMENT_WORKFLOW = "development-codex.yml"
GITHUB_DEVELOPMENT_TIMEOUT_SECONDS = 3
GITHUB_DEVELOPMENT_PROMPT_MAX_BYTES = 40000
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
_TEST_PRIVATE_MEDIA = tempfile.TemporaryDirectory(prefix="service2-test-private-")
_TEST_PUBLIC_MEDIA = tempfile.TemporaryDirectory(prefix="service2-test-media-")
PRIVATE_MEDIA_ROOT = _TEST_PRIVATE_MEDIA.name
MEDIA_ROOT = _TEST_PUBLIC_MEDIA.name

COMMUNICATION_RECORDING_DOWNLOAD_INLINE = False
COMMUNICATION_CLIENT_ENRICHMENT_ENABLED = False
