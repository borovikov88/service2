import json
import logging

from django.conf import settings
from django.templatetags.static import static
from pywebpush import WebPushException, webpush

from pool_service.models import PushSubscription

logger = logging.getLogger(__name__)


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


def _icon_url():
    base = getattr(settings, "SITE_URL", "").rstrip("/")
    if not base:
        return ""
    return f"{base}{static('assets/images/favicon.png')}"


def send_push_to_users(users, *, title, message, action_url=""):
    config = _push_config()
    if not config:
        return 0
    icon_url = _icon_url()
    payload = {
        "title": title,
        "body": message,
        "url": action_url,
        "icon": icon_url,
    }
    sent = 0
    for user in users:
        subscriptions = PushSubscription.objects.filter(user=user)
        for sub in subscriptions:
            if not sub.endpoint or not sub.p256dh or not sub.auth:
                sub.delete()
                continue
            subscription_info = {
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
            }
            try:
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
                    logger.warning("Web push failed for user %s: %s", user.id, exc)
                continue
            except Exception:
                logger.exception("Web push failed for user %s", user.id)
                continue
    return sent
