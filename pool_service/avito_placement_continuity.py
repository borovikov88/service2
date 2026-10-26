"""Read-only continuity projection; never publishes, activates, or infers a fee."""
from datetime import date, timedelta
import re
from zoneinfo import ZoneInfo
from django.utils import timezone
from django.utils.dateparse import parse_datetime

ZONE = ZoneInfo("Asia/Barnaul")
STATUSES = {"active", "old", "removed", "blocked", "rejected"}


def aware_time(value):
    if (not isinstance(value, str) or len(value) > 50
            or re.search(r"-00(?::?00)?$", value)):
        return None
    try:
        parsed = parse_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and timezone.is_aware(parsed) else None


def source_calendar_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def classify_finish(value):
    """Classify the existing reader-normalized field; never expose its raw value."""
    value_type = ("null" if value is None else "boolean" if type(value) is bool
        else "string" if isinstance(value, str) else "integer" if type(value) is int
        else "number" if type(value) is float else "array" if type(value) is list
        else "object" if type(value) is dict else "other")
    shape = {"stage": "reader_normalized", "value_type": value_type,
        "length": min(len(value), 51) if isinstance(value, str) else None,
        "format": "missing" if value is None else "empty" if value == "" else "not_text"}
    result = {"source": "avito_item_detail.finish_time", "finish_state": "not_returned",
        "finish_time": None, "source_finish_date": None, "shape": shape}
    if value is None or value == "":
        return result
    if not isinstance(value, str):
        result["finish_state"] = "invalid_type"
        return result
    shape["format"] = "invalid"
    result["finish_state"] = "invalid_format"
    if len(value) > 50:
        shape["format"] = "over_limit"
        return result
    calendar = source_calendar_date(value)
    if calendar is not None:
        result.update(finish_state="calendar_date_only", source_finish_date=calendar.isoformat())
        shape["format"] = "date_only"
        return result
    try:
        parsed = parse_datetime(value)
    except (ValueError, TypeError, OverflowError):
        parsed = None
    if parsed is None:
        return result
    if timezone.is_naive(parsed) or re.search(r"-00(?::?00)?$", value):
        result.update(finish_state="timezone_missing", source_finish_date=parsed.date().isoformat())
        shape["format"] = "naive_datetime" if timezone.is_naive(parsed) else "unknown_offset_datetime"
        return result
    result.update(finish_state="known", finish_time=parsed.isoformat())
    shape["format"] = "aware_datetime"
    return result


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
        evidence = observation.get("finish_evidence") if valid else None
        evidence = evidence if isinstance(evidence, dict) else {}
        calendar = (source_calendar_date(evidence.get("source_finish_date"))
            if evidence.get("source") == "avito_item_detail.finish_time"
            and evidence.get("finish_state") in {"timezone_missing", "calendar_date_only"}
            and evidence.get("finish_time") is None else None)
        days = (finish.astimezone(ZONE).date() - today).days if finish else None
        owner_withdrawn = row.get("archive_control", {}).get("owner_withdrawn") is True
        if owner_withdrawn:
            state, label = "owner_withdrawn", "Возврат запрещён владельцем"
        elif status == "active" and finish is not None and finish <= now:
            state, label = "refresh_required", "Сохранённый срок прошёл: обновите статус"
        elif status == "active" and days is not None and days <= 7:
            state, label = "due_soon", "Подготовить повторное размещение"
        elif status == "active" and calendar is not None:
            state, label = "timezone_unknown", "Дата источника известна; часовой пояс и точный момент не подтверждены"
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
            "status": status, "finish_time": finish, "source_finish_date": calendar,
            "days_remaining": days,
            "finish_source": "avito_item_detail.finish_time" if finish else None,
            "checked_at": checked if valid else None, "can_reactivate": False,
            "additional_cost": None})
    return {"connection_id": 4, "account_id": "446312402", "rows": projected,
        "source": "service2_saved_owned_item_observations", "timezone": "Asia/Barnaul",
        "due_count": sum(row["state"] == "due_soon" for row in projected),
        "unknown_count": sum(row["status"] is None or row["finish_time"] is None for row in projected),
        "automatic_reactivation": False, "provider_request_performed": False}


PARSER_VERSION = "owned_finish_calendar_2026_10_26_v1"
CHECK_KEY = "avito_owned_finish_calendar_check"


def _check_receipt(value):
    if not isinstance(value, dict) or type(value.get("connection_id")) is not int or value["connection_id"] != 4:
        return None
    checked = aware_time(value.get("checked_at"))
    counts = {key: value.get(key) for key in ("history_event_id", "observed_count", "known_instants", "source_calendar_dates")}
    if (checked is None or checked > timezone.now() + timedelta(minutes=5)
            or any(type(v) is not int or not 0 <= v <= 10**9 for v in counts.values())
            or not 1 <= counts["observed_count"] <= 200 or counts["history_event_id"] == 0
            or counts["known_instants"] + counts["source_calendar_dates"] > counts["observed_count"]
            or any(value.get(key) is not False for key in ("draft_changed", "published_changed", "provider_write_performed"))):
        return None
    return {"connection_id": 4, "checked_at": checked.isoformat(), **counts,
        "draft_changed": False, "published_changed": False, "provider_write_performed": False}


def prepare_once():
    """One scoped read/save using the existing checker, called only by protected deployment."""
    from copy import deepcopy
    import uuid
    from django.core.exceptions import PermissionDenied
    from django.db import transaction
    from .communication_avito import AvitoError
    from .communication_models import ChannelConnection
    from .avito_feed_models import AvitoServicesFeed
    from .avito_services_feed import verify_owned_observations, _publication_connection, PublicationConflict
    from .avito_services_workplace_setup import _connection, _actor, _binding
    initial = _connection(4)
    actor = _actor(initial)
    with transaction.atomic():
        current, actor = _publication_connection(4, actor, locked=True)
        feed = AvitoServicesFeed.objects.get(connection=current)
        if not scope_matches(current, feed):
            return {"state": "blocked", "code": "placement_scope_changed"}
        binding = _binding(current)
        metadata = deepcopy(current.settings) if isinstance(current.settings, dict) else {}
        saved = metadata.get(CHECK_KEY, {})
        saved = saved if isinstance(saved, dict) else {}
        if saved.get("binding") == binding and saved.get("version") == PARSER_VERSION:
            receipt = _check_receipt(saved.get("receipt"))
            if saved.get("state") == "verified" and receipt is not None:
                return {"state": "already_verified", **receipt}
            until = aware_time(saved.get("lease_until"))
            if until is not None and until > timezone.now():
                return {"state": "pending", "code": "placement_check_busy"}
        lease = uuid.uuid4().hex
        metadata[CHECK_KEY] = {"version": PARSER_VERSION, "binding": binding,
            "state": "checking", "lease": lease,
            "lease_until": (timezone.now() + timedelta(seconds=180)).isoformat()}
        current.settings = metadata
        current.save(update_fields=["settings"])
        revision = feed.revision

    def owns_lease(current, candidate):
        metadata = current.settings if isinstance(current.settings, dict) else {}
        marker = metadata.get(CHECK_KEY, {})
        if not isinstance(marker, dict):
            return False
        until = aware_time(marker.get("lease_until"))
        return bool(scope_matches(current, candidate) and _binding(current) == binding
            and marker.get("version") == PARSER_VERSION and marker.get("lease") == lease
            and marker.get("binding") == binding and until is not None and until > timezone.now())

    try:
        result = verify_owned_observations(current, actor, expected_revision=revision, expected_guard=owns_lease)
    except (AvitoError, ValueError, PermissionDenied) as error:
        from .avito_readonly_audit import safe_error
        code = (safe_error(error) if isinstance(error, AvitoError) else
                "placement_access_changed" if isinstance(error, PermissionDenied) else "placement_check_changed")
        # A failed read must not permanently claim completion; lease expires naturally.
        return {"state": "pending" if not isinstance(error, PermissionDenied) else "blocked", "code": code,
            "provider_write_performed": False, "draft_changed": False, "published_changed": False}
    receipt = _check_receipt(result)
    if receipt is None:
        return {"state": "blocked", "code": "placement_receipt_invalid"}
    with transaction.atomic():
        current, actor = _publication_connection(4, actor, locked=True)
        feed = AvitoServicesFeed.objects.get(connection=current)
        if not owns_lease(current, feed):
            return {"state": "pending", "code": "placement_check_changed",
                    "provider_write_performed": False, "draft_changed": False, "published_changed": False}
        metadata = deepcopy(current.settings) if isinstance(current.settings, dict) else {}
        metadata[CHECK_KEY] = {"version": PARSER_VERSION, "binding": binding,
            "state": "verified", "receipt": receipt}
        current.settings = metadata
        current.save(update_fields=["settings"])
    return {"state": "verified", **receipt}
