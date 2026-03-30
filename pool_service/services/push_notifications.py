import json
import logging
from contextlib import contextmanager
from urllib.parse import urlparse

from django.conf import settings
from django.core import signing
from django.templatetags.static import static
from django.urls import reverse
import pywebpush
from pywebpush import WebPushException, webpush

from pool_service.models import PushSubscription

logger = logging.getLogger(__name__)


@contextmanager
def _patch_pywebpush_generate_private_key():
    original = pywebpush.ec.generate_private_key

    def _compat_generate_private_key(curve, backend=None):
        if isinstance(curve, type):
            curve = curve()
        return original(curve, backend)

    pywebpush.ec.generate_private_key = _compat_generate_private_key
    try:
        yield
    finally:
        pywebpush.ec.generate_private_key = original


def _push_config():
    public_key = getattr(settings, "VAPID_PUBLIC_KEY", "")
    private_key = getattr(settings, "VAPID_PRIVATE_KEY", "")
    if not public_key or not private_key:
        return None
    email = getattr(settings, "VAPID_EMAIL", "")
    return {
        "public_key": public_key,
        "private_key": private_key,
        "email": email,
    }


def _normalize_host(host):
    return (host or "").split(":", 1)[0].strip().lower()


def _base_url_for_host(host):
    host = _normalize_host(host)
    if not host:
        configured = getattr(settings, "SITE_URL", "").rstrip("/")
        if configured:
            return configured
        return ""
    return f"https://{host}"


def _icon_path_for_host(host):
    host = _normalize_host(host)
    if host in {"service2.aqualine22.ru", "www.service2.aqualine22.ru"}:
        return static("assets/images/aqualine-favicon.png")
    return static("assets/images/rovikpool-favicon.png")


def _absolute_url_for_host(host, path):
    base = _base_url_for_host(host)
    if not base:
        return path or ""
    if not path:
        return base
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{base}{path}"


def _notification_open_url(notification):
    token = signing.dumps({"notification_id": notification.id, "user_id": notification.user_id})
    return reverse("notification_push_open_token", kwargs={"token": token})


def send_push_to_users(users, *, title, message, action_url="", notification=None):
    config = _push_config()
    if not config:
        return 0
    active_users = [user for user in users if user and user.is_active]
    if not active_users:
        return 0
    sent = 0
    subscriptions = PushSubscription.objects.filter(user__in=active_users).select_related("user")
    for sub in subscriptions:
        if not sub.endpoint or not sub.p256dh or not sub.auth:
            sub.delete()
            continue
        subscription_info = {
            "endpoint": sub.endpoint,
            "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
        }
        host = _normalize_host(sub.host) or (urlparse(getattr(settings, "SITE_URL", "")).hostname or "")
        payload = {
            "title": title,
            "body": message,
            "url": _notification_open_url(notification) if notification else action_url,
            "icon": _absolute_url_for_host(host, _icon_path_for_host(host)),
        }
        try:
            with _patch_pywebpush_generate_private_key():
                webpush(
                    subscription_info=subscription_info,
                    data=json.dumps(payload),
                    vapid_private_key=config["private_key"],
                    vapid_claims={"sub": f"mailto:{config['email']}" if config["email"] else "mailto:admin@localhost"},
                    ttl=3600,
                )
            sent += 1
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in {404, 410}:
                sub.delete()
            else:
                logger.warning("Web push failed for user %s: %s", sub.user_id, exc)
            continue
        except Exception:
            logger.exception("Web push failed for user %s", sub.user_id)
            continue
    return sent
