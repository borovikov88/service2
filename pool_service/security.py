from urllib.parse import quote

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone


SESSION_LOCKED_KEY = "security_locked"
SESSION_LAST_ACTIVITY_KEY = "security_last_activity"
SESSION_LOCKED_AT_KEY = "security_locked_at"
SESSION_NEXT_KEY = "security_next"

DEFAULT_IDLE_TIMEOUT_SECONDS = 5 * 60
MAX_PIN_ATTEMPTS = 5


def idle_timeout_seconds():
    return int(getattr(settings, "SECURITY_IDLE_TIMEOUT_SECONDS", DEFAULT_IDLE_TIMEOUT_SECONDS))


def timestamp_now():
    return int(timezone.now().timestamp())


def has_security_pin(user):
    profile = getattr(user, "profile", None)
    return bool(profile and profile.security_pin_hash)


def set_security_pin(profile, pin):
    profile.security_pin_hash = make_password(pin)
    profile.security_pin_failed_attempts = 0
    profile.security_pin_changed_at = timezone.now()
    profile.save(
        update_fields=[
            "security_pin_hash",
            "security_pin_failed_attempts",
            "security_pin_changed_at",
        ]
    )


def clear_security_pin(profile):
    profile.security_pin_hash = ""
    profile.security_pin_failed_attempts = 0
    profile.security_pin_changed_at = None
    profile.save(
        update_fields=[
            "security_pin_hash",
            "security_pin_failed_attempts",
            "security_pin_changed_at",
        ]
    )


def verify_security_pin(profile, pin):
    return bool(profile.security_pin_hash and check_password(pin, profile.security_pin_hash))


def mark_session_unlocked(request):
    request.session[SESSION_LOCKED_KEY] = False
    request.session[SESSION_LAST_ACTIVITY_KEY] = timestamp_now()
    request.session.pop(SESSION_LOCKED_AT_KEY, None)
    request.session.modified = True


def lock_session(request, next_url=""):
    request.session[SESSION_LOCKED_KEY] = True
    request.session[SESSION_LOCKED_AT_KEY] = timestamp_now()
    if next_url:
        request.session[SESSION_NEXT_KEY] = next_url
    request.session.modified = True


def unlock_url(next_url=""):
    if next_url:
        return f"/security/unlock/?next={quote(next_url)}"
    return "/security/unlock/"
