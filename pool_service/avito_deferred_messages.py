"""Owner-only Avito message quiet hours; retain messages and a durable range."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from .communication_models import ChannelConnection, ConversationMessage
from .avito_status_monitor import subscriber_allowed
from .models import Notification, Profile

KEY = "avito_deferred_owner_messages"
ZONE = ZoneInfo("Asia/Barnaul")


def daylight(now=None):
    return 8 <= (now or timezone.now()).astimezone(ZONE).hour < 21


def _marker(connection):
    value = connection.settings.get("avito_services_workplace_v1") if isinstance(connection.settings, dict) else None
    return value if isinstance(value, dict) else {}


def allowed_actor(connection, state):
    if (connection.pk != 4 or connection.external_id != "446312402" or not connection.is_active
            or not connection.channel.is_active or connection.channel.kind != "avito"
            or state.get("account_id") != connection.external_id
            or state.get("organization_id") != connection.channel.organization_id
            or state.get("channel_id") != connection.channel_id
            or type(state.get("actor_id")) is not int):
        return None
    actor = User.objects.filter(pk=state["actor_id"], is_active=True).first()
    return actor if actor and subscriber_allowed(actor, connection.channel.organization) else None


def partition_users(connection, message, users):
    """Caller locks connection before conversation; duplicates never reach this."""
    users = list(users)
    marker = _marker(connection)
    actor = allowed_actor(connection, marker)
    if daylight() or actor is None or actor.pk not in {user.pk for user in users}:
        return users
    metadata = dict(connection.settings)
    queued = metadata.get(KEY)
    queued = dict(queued) if isinstance(queued, dict) else {}
    if queued and any(queued.get(key) != marker.get(key) for key in
                      ("actor_id", "organization_id", "channel_id", "account_id")):
        # Retain unresolved historical work instead of moving it to a new actor.
        return [user for user in users if user.pk != actor.pk]
    if queued and (type(queued.get("first_id")) is not int or type(queued.get("last_id")) is not int):
        queued["last_error_code"] = "deferred_queue_invalid"
        metadata[KEY] = queued
        connection.settings = metadata
        connection.save(update_fields=["settings"])
        return [user for user in users if user.pk != actor.pk]
    now = timezone.now().astimezone(ZONE)
    morning = now.replace(hour=8, minute=0, second=0, microsecond=0)
    if now.hour >= 21:
        morning += timedelta(days=1)
    queued.update(actor_id=actor.pk, organization_id=connection.channel.organization_id,
        channel_id=connection.channel_id, account_id=connection.external_id,
        first_id=min(queued.get("first_id", message.pk), message.pk),
        last_id=max(queued.get("last_id", message.pk), message.pk),
        next_due_at=morning.isoformat(), last_error_code="")
    metadata[KEY] = queued
    connection.settings = metadata
    connection.save(update_fields=["settings"])
    return [user for user in users if user.pk != actor.pk]


def flush(connection_id):
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related("channel__organization").get(pk=connection_id)
        metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
        queued = metadata.get(KEY)
        if not isinstance(queued, dict) or not queued or not daylight():
            return "skipped"
        actor = allowed_actor(connection, queued)
        from .communication_services import users_with_conversation_access
        if actor is None or not users_with_conversation_access(connection.channel.organization).filter(pk=actor.pk).exists():
            queued["last_error_code"] = "binding_changed" if actor is None else "conversation_access_denied"
            metadata[KEY] = queued
            connection.settings = metadata
            connection.save(update_fields=["settings"])
            return "failed"
        first, last = queued.get("first_id"), queued.get("last_id")
        if type(first) is not int or type(last) is not int or not 0 < first <= last:
            queued["last_error_code"] = "deferred_queue_invalid"
            metadata[KEY] = queued
            connection.settings = metadata
            connection.save(update_fields=["settings"])
            return "failed"
        key = f"avito-night:{connection.pk}:{actor.pk}:{first}:{last}"
        from .services.notifications import notify_users
        from django.urls import reverse
        count = ConversationMessage.objects.filter(conversation__connection=connection,
            conversation__organization_id=connection.channel.organization_id,
            direction=ConversationMessage.DIRECTION_IN, pk__gte=first, pk__lte=last).count()
        if count:
            notify_users([actor], organization=connection.channel.organization, kind="communication",
                title="Авито: сообщения за тихие часы",
                message=f"В подключении услуг получено сообщений: {count}. Проверьте диалоги и неотвеченные обращения.",
                action_url=reverse("communications_conversations"), dedupe_key=key, send_push=False)
            if not Notification.objects.filter(user=actor, organization=connection.channel.organization,
                    kind="communication", dedupe_key=key).exists():
                queued["last_error_code"] = "notification_preferences_disabled"
                metadata[KEY] = queued
                connection.settings = metadata
                connection.save(update_fields=["settings"])
                return "failed"
        metadata.pop(KEY, None)
        connection.settings = metadata
        connection.save(update_fields=["settings"])
        return "success"


def scan_due(*, observer=None):
    counts = {"success": 0, "failed": 0, "skipped": 0}
    # This release changes notification policy only for the explicitly opted-in services account.
    if not ChannelConnection.objects.filter(pk=4, external_id="446312402",
            settings__avito_deferred_owner_messages__isnull=False).exists():
        return counts
    raised = False
    try:
        outcome = flush(4)
    except Exception:
        outcome, raised = "failed", True
    counts[outcome] += 1
    if observer:
        reason = "quiet_hours" if outcome == "skipped" and not daylight() else "not_configured" if outcome == "skipped" else None
        observer(4, "messages_digest", outcome, exception=raised, reason=reason)
    return counts
