"""Read-only continuity projection; never publishes, activates, or infers a fee."""
from datetime import timedelta
from zoneinfo import ZoneInfo
from django.utils import timezone
from django.utils.dateparse import parse_datetime

ZONE = ZoneInfo("Asia/Barnaul")
STATUSES = {"active", "old", "removed", "blocked", "rejected"}


def aware_time(value):
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        parsed = parse_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and timezone.is_aware(parsed) else None


def scope_matches(connection, feed):
    return bool(feed is not None and connection.pk == 4
        and connection.external_id == "446312402" and connection.is_active
        and connection.channel.kind == "avito" and connection.channel.is_active
        and feed.connection_id == connection.pk
        and feed.account_id == connection.external_id
        and feed.organization_id_at_import == connection.channel.organization_id)


def continuity_context(connection, feed, rows, *, now=None):
    """Use saved owned observations only; opening the editor makes no API call."""
    if not scope_matches(connection, feed):
        return None
    now = now or timezone.now()
    today = now.astimezone(ZONE).date()
    projected = []
    for row in rows:
        observation = row.get("observation") or {}
        checked = aware_time(observation.get("checked_at"))
        valid = (observation.get("avito_id") == row["AvitoId"]
            and observation.get("id") == row["Id"]
            and observation.get("source") == "avito_item_detail"
            and observation.get("status") in STATUSES and checked is not None
            and checked <= now + timedelta(minutes=5))
        status = observation["status"] if valid else None
        finish = aware_time(observation.get("finish_time")) if valid else None
        if observation.get("finish_time_source") != "avito_item_detail.finish_time":
            finish = None
        days = (finish.astimezone(ZONE).date() - today).days if finish else None
        owner_withdrawn = row.get("archive_control", {}).get("owner_withdrawn") is True
        if owner_withdrawn:
            state, label = "owner_withdrawn", "Возврат запрещён владельцем"
        elif status == "active" and finish is not None and finish <= now:
            state, label = "refresh_required", "Сохранённый срок прошёл: обновите статус"
        elif status == "active" and days is not None and days <= 7:
            state, label = "due_soon", "Подготовить повторное размещение"
        elif status == "active":
            state, label = "active", "Размещено по сохранённой проверке"
        elif status == "old":
            state, label = "archive_unknown", "Архив: сначала подтвердите причину"
        elif status == "removed":
            state, label = "removed", "Снято: инициатор не подтверждён"
        elif status in {"blocked", "rejected"}:
            state, label = status, "Нужно устранить причину в Авито"
        else:
            state, label = "unknown", "Нужно получить статус"
        projected.append({"avito_id": row["AvitoId"], "state": state, "label": label,
            "status": status, "finish_time": finish, "days_remaining": days,
            "finish_source": "avito_item_detail.finish_time" if finish else None,
            "checked_at": checked if valid else None, "can_reactivate": False,
            "additional_cost": None})
    return {"connection_id": 4, "account_id": "446312402", "rows": projected,
        "source": "service2_saved_owned_item_observations", "timezone": "Asia/Barnaul",
        "due_count": sum(row["state"] == "due_soon" for row in projected),
        "unknown_count": sum(row["status"] is None or row["finish_time"] is None for row in projected),
        "automatic_reactivation": False, "provider_request_performed": False}
