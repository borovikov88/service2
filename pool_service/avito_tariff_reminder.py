"""Personal reminders for an explicitly entered renewal date, never an API tariff claim."""
import re
import uuid
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.avito_scheduler import scheduler_state
from pool_service.avito_status_monitor import subscriber_allowed
from pool_service.communication_models import ChannelConnection
from pool_service.models import Notification, Profile
from pool_service.services.notifications import notify_users
from pool_service.avito_deferred_messages import daylight

KEY = "avito_tariff_reminder"
INTERVAL = timedelta(minutes=15)
SERVICES_ZONE = ZoneInfo("Asia/Barnaul")
MANUAL_SOURCE = "avito_cabinet_manual"
ERROR_BINDING = "Настройки подключения или права изменились. Включите напоминания повторно."
ERROR_PREFS = "Уведомления внутри Service2 выключены в вашем профиле. Напоминание не считается доставленным."


def zone():
    return ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))


def today():
    return timezone.localdate(timezone=zone())


def _services(connection):
    return connection.pk == 4 and str(connection.external_id) == "446312402"


def _today_for(connection):
    return timezone.localdate(timezone=SERVICES_ZONE) if _services(connection) else today()


def _date(value):
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


def _time(value):
    try:
        parsed = parse_datetime(value) if isinstance(value, str) else None
        return parsed if parsed and timezone.is_aware(parsed) else None
    except ValueError:
        return None


def _state(connection):
    metadata = connection.settings if isinstance(connection.settings, dict) else {}
    value = metadata.get(KEY)
    return dict(value) if isinstance(value, dict) else {}


def _save(connection, state):
    metadata = dict(connection.settings) if isinstance(connection.settings, dict) else {}
    metadata[KEY] = state
    connection.settings = metadata
    connection.save(update_fields=["settings"])


def _in_app(user_id):
    value = Profile.objects.filter(user_id=user_id).values_list("in_app_notifications_enabled", flat=True).first()
    return value is not False


def _allowed(connection, state):
    actor_id = state.get("actor_id")
    if type(actor_id) is not int or actor_id < 1:
        return False
    actor = User.objects.filter(pk=actor_id, is_active=True).first()
    return bool(
        actor and isinstance(state.get("generation"), str)
        and re.fullmatch(r"[0-9a-f]{32}", state["generation"])
        and connection.channel.kind == "avito"
        and connection.is_active and connection.channel.is_active
        and state.get("source") == "manual"
        and _date(state.get("expires_on"))
        and state.get("organization_id") == connection.channel.organization_id
        and state.get("channel_id") == connection.channel_id
        and state.get("account_id") == str(connection.external_id)
        and subscriber_allowed(actor, connection.channel.organization)
    )


def _scheduler():
    value = scheduler_state()
    return {**value, "ready": value.get("worker_ready", value["ready"])}


def configure(connection, user, action, *, expires_on=None):
    if action not in {"enable", "disable", "retry", "save_date"}:
        raise ValueError("Неизвестное действие.")
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection.pk)
        if connection.channel.kind != "avito" or not subscriber_allowed(user, connection.channel.organization):
            raise PermissionDenied
        if action == "save_date" and not _services(connection):
            raise ValueError("Отдельное сохранение даты доступно только для подключения услуг №4.")
        state = _state(connection)
        if state.get("actor_id") is not None and state["actor_id"] != user.pk:
            raise PermissionDenied
        if action == "disable":
            state.update(enabled=False, generation=uuid.uuid4().hex)
        elif action == "retry":
            if state.get("enabled") is not True or not _allowed(connection, state):
                raise ValueError("Сначала включите напоминания о продлении.")
            state["next_due_at"] = timezone.now().isoformat()
        else:
            if type(expires_on) is not date or not _today_for(connection) - timedelta(days=366) <= expires_on <= _today_for(connection) + timedelta(days=3660):
                raise ValueError("Укажите дату окончания тарифа из кабинета Авито.")
            if not connection.is_active or not connection.channel.is_active:
                raise ValueError("Нужно активное подключение Авито.")
            if action == "enable":
                if not _in_app(user.pk):
                    raise ValueError(ERROR_PREFS)
                if not _scheduler()["ready"]:
                    raise ValueError("Сначала подтвердите работу серверного расписания.")
            valid = _allowed(connection, state)
            previously_enabled = state.get("enabled") is True and valid
            if (not valid or state.get("expires_on") != expires_on.isoformat()
                    or (action == "enable" and not previously_enabled)):
                state = {
                    "enabled": action == "enable" or previously_enabled, "generation": uuid.uuid4().hex,
                    "actor_id": user.pk, "organization_id": connection.channel.organization_id,
                    "channel_id": connection.channel_id, "account_id": str(connection.external_id),
                    "expires_on": expires_on.isoformat(), "source": "manual",
                    "next_due_at": timezone.now().isoformat() if action == "enable" or previously_enabled else None,
                    "last_error_detail": "",
                }
            if _services(connection):
                # Only an explicit authenticated save confirms a supplied date.
                # Same-date saves refresh provenance without rearming delivery.
                state.update(confirmation_source=MANUAL_SOURCE,
                             confirmed_at=timezone.now().isoformat(), confirmed_by=user.pk)
        _save(connection, state)
    return state


def display_state(connection, user):
    if not connection or not subscriber_allowed(user, connection.channel.organization):
        return {}
    state = _state(connection)
    if state.get("actor_id") is not None and state["actor_id"] != user.pk:
        return {}
    services = _services(connection)
    confirmed = (_time(state.get("confirmed_at")) if services and _allowed(connection, state)
                 and state.get("confirmation_source") == MANUAL_SOURCE
                 and state.get("confirmed_by") == user.pk else None)
    quiet_until = _time(state.get("quiet_until_at")) if services and _allowed(connection, state) else None
    return {
        "allowed": True, "enabled": state.get("enabled") is True,
        "needs_reactivation": state.get("enabled") is True and not _allowed(connection, state),
        "expires_on": _date(state.get("expires_on")),
        "last_checked_at": _time(state.get("last_checked_at")),
        "last_error_detail": state.get("last_error_detail", ""),
        "in_app_enabled": _in_app(user.pk), "timezone": str(SERVICES_ZONE if services else zone()), "scheduler": _scheduler(),
        "services_schedule": services,
        "confirmation_source_label": "Введено вручную из кабинета Авито" if confirmed else "",
        "confirmed_at_display": confirmed.astimezone(SERVICES_ZONE).strftime("%d.%m.%Y %H:%M:%S") if confirmed else "",
        "quiet_until_display": quiet_until.astimezone(SERVICES_ZONE).strftime("%d.%m.%Y %H:%M:%S") if quiet_until else "",
    }


def _stage(expires_on, current_date, *, include_week=False):
    days = (expires_on - current_date).days
    return ("due" if days <= 0 else "tomorrow" if days == 1 else "soon" if days <= 3
            else "week" if include_week and days <= 7 else "")


def scan_connection(connection_id):
    with transaction.atomic():
        connection = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection_id)
        state, now = _state(connection), timezone.now()
        due = _time(state.get("next_due_at"))
        if state.get("enabled") is not True or due is None or due > now:
            return "skipped"
        if not _allowed(connection, state):
            state.update(enabled=False, last_error_detail=ERROR_BINDING)
            _save(connection, state)
            return "failed"
        if _services(connection) and not daylight(now):
            local = now.astimezone(SERVICES_ZONE)
            morning = local.replace(hour=8, minute=0, second=0, microsecond=0)
            if local.hour >= 21:
                morning += timedelta(days=1)
            state.update(next_due_at=morning.isoformat(), quiet_until_at=morning.isoformat(),
                         last_checked_at=now.isoformat())
            _save(connection, state)
            return "skipped"
        state.pop("quiet_until_at", None)
        state.update(next_due_at=(now + INTERVAL).isoformat(), last_checked_at=now.isoformat())
        if not _in_app(state["actor_id"]):
            state["last_error_detail"] = ERROR_PREFS
            _save(connection, state)
            return "failed"
        expiry = _date(state["expires_on"])
        stage = _stage(expiry, _today_for(connection), include_week=_services(connection))
        if stage and state.get("last_stage") != stage:
            actor = User.objects.get(pk=state["actor_id"])
            label = {"week": "Указанная дата окончания тарифа — в ближайшие 7 дней",
                     "soon": "Дата продления тарифа приближается",
                     "tomorrow": "Указанная дата окончания тарифа — завтра",
                     "due": "Наступила указанная дата окончания тарифа"}[stage]
            if _services(connection) and stage == "soon":
                label = "Указанная дата окончания тарифа — в ближайшие 3 дня"
            dedupe = f"avito-tariff:{connection.pk}:{state['generation']}:{stage}"
            notify_users(
                [actor], organization=connection.channel.organization, kind="communication",
                level="danger" if stage == "due" else "warning", send_push=False,
                title=f"Авито: {label.lower()}",
                message=f"{connection.name}: вы указали дату {expiry:%d.%m.%Y}. Проверьте продление в Авито. Дата введена вручную; фактический статус подписки не подтверждён API.",
                action_url=reverse("avito_dashboard") + f"?account={connection.pk}#avito-tariff",
                dedupe_key=dedupe,
            )
            if not Notification.objects.filter(
                user_id=actor.pk, organization_id=connection.channel.organization_id,
                kind="communication", dedupe_key=dedupe,
            ).exists():
                state["last_error_detail"] = ERROR_PREFS
                _save(connection, state)
                return "failed"
            state["last_stage"] = stage
        state["last_error_detail"] = ""
        _save(connection, state)
        return "success"


def scan_due(*, deadline, limit=4, observer=None):
    import time
    counts = {"success": 0, "failed": 0, "skipped": 0}
    ids = list(ChannelConnection.objects.filter(
        channel__kind="avito", settings__avito_tariff_reminder__enabled=True,
        settings__avito_tariff_reminder__next_due_at__lte=timezone.now().isoformat(),
    ).order_by("settings__avito_tariff_reminder__next_due_at", "pk").values_list("pk", flat=True)[:limit])
    for connection_id in ids:
        if deadline - time.monotonic() < 5:
            break
        failed_with_exception = False
        try:
            outcome = scan_connection(connection_id)
        except Exception:
            failed_with_exception = True
            outcome = "failed"
        counts[outcome] += 1
        if observer is not None:
            observer(connection_id, "tariff", outcome, exception=failed_with_exception)
    return counts
