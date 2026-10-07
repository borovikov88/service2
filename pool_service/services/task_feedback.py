"""Human task updates. Comments never silently change task state.

The existing task and change log are the durable source of truth. This module
uses the same task/CRM lock order as Operations MCP and does not run any AI.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone as datetime_timezone
from uuid import UUID
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from pool_service.models import OrganizationAccess, ServiceTask, ServiceTaskChange
from pool_service.services.crm_locking import locked_task_with_crm_graph
from pool_service.services.task_archive import archive_task
from pool_service.services.task_feedback_permissions import lock_feedback_actor
from pool_service.services.task_waiting_schedule import finish_waiting_check, schedule_waiting_check

NAMESPACE = "task_feedback"
EVENT_SCHEMA = "service2.task-feedback.v1"
ACTIONS = frozenset({"comment", "reschedule", "wait", "cancel", "complete"})
ACTION_LABELS = {
    "comment": "Комментарий", "reschedule": "Перенос срока",
    "wait": "Ожидаем", "cancel": "Отменена", "complete": "Выполнена",
}


class FeedbackConflict(ValidationError):
    pass


def state_for(task):
    payload = task.payload_json if isinstance(task.payload_json, dict) else {}
    state = payload.get(NAMESPACE)
    return dict(state) if isinstance(state, dict) else {}


def _iso(value):
    # A newly saved task may retain a local offset while a database reload
    # returns UTC. Hash the instant, not its timezone representation. Keep
    # date-only/time-only values and microseconds unchanged.
    if isinstance(value, datetime) and timezone.is_aware(value):
        value = value.astimezone(datetime_timezone.utc)
    return value.isoformat() if value is not None else None


def snapshot(task):
    return {
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "start_date": _iso(task.start_date), "end_date": _iso(task.end_date),
        "start_time": _iso(task.start_time), "end_time": _iso(task.end_time),
        "due_at": _iso(task.due_at), "completed_at": _iso(task.completed_at),
        "is_archived": task.is_archived,
        "primary_responsible_id": task.primary_responsible_id,
        "responsibles": sorted(task.responsibles.values_list("id", flat=True)),
        "feedback": state_for(task),
    }


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def version_for(task):
    # Notification-delivery retries do not invalidate an otherwise current form.
    # Human edits, participants, dates, archival and feedback do invalidate it.
    return _digest(snapshot(task))


def has_access(task, user, *, write=False):
    if not user or not user.is_authenticated or not user.is_active:
        return False
    if not task.organization_id or not OrganizationAccess.objects.filter(
        organization_id=task.organization_id, user_id=user.pk,
    ).exists():
        return False
    from pool_service.views import _task_can_edit, _task_can_view
    check = _task_can_edit if write else _task_can_view
    return bool(check(task, user))


def _aware_local(value):
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValidationError("Некорректные дата и время.")
    if timezone.is_naive(value):
        zone = ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))
        value = timezone.make_aware(value, zone)
    return value


def waiting_control(task):
    """Return (waiting-without-deadline, next internal check).

An explicit subsequently edited end_date takes precedence over an old waiting
marker. Never fall back to the original appointment when its date is unknown.
"""
    state = state_for(task)
    waiting = (
        task.status == ServiceTask.STATUS_WAITING
        and state.get("mode") == "waiting"
        and task.end_date is None
    )
    if not waiting:
        return False, None
    value = state.get("next_check_at")
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else None
        if parsed is None or timezone.is_naive(parsed):
            return True, None
        return True, parsed
    except (TypeError, ValueError):
        return True, None


def history_for(task, *, limit=100):
    entries = []
    for change in task.changes.filter(field_name__startswith="feedback:").select_related("changed_by").order_by("-id")[:limit]:
        try:
            event = json.loads(change.new_value)
        except (TypeError, ValueError):
            continue
        if not isinstance(event, dict) or event.get("schema") != EVENT_SCHEMA:
            continue
        entries.append({
            "id": change.id, "created_at": change.created_at,
            "author": (change.changed_by.get_full_name() or change.changed_by.username) if change.changed_by else "Система",
            "action": ACTION_LABELS.get(event.get("action"), "Изменение"),
            "comment": event.get("comment", ""),
            "due_date": event.get("due_date"), "due_time": event.get("due_time"),
            "next_check_at": event.get("next_check_at"),
            "revision": event.get("revision"),
        })
    return entries


@transaction.atomic
def apply_feedback(*, task_id, user, action, comment, expected_version, request_id,
                   due_date=None, due_time=None, next_check_at=None):
    """Apply one explicit user action atomically, with replay and stale-edit guards."""
    if action not in ACTIONS:
        raise ValidationError("Неизвестное действие.")
    if not isinstance(comment, str) or not comment.strip() or len(comment.strip()) > 2000:
        raise ValidationError("Добавьте комментарий или причину, не более 2000 символов.")
    comment = comment.strip()
    try:
        request_id = str(UUID(str(request_id)))
    except (TypeError, ValueError, AttributeError):
        raise ValidationError("Некорректный идентификатор запроса.")
    next_check_at = _aware_local(next_check_at)
    if action == "wait" and (next_check_at is None or next_check_at <= timezone.now()):
        raise ValidationError("Укажите будущие дату и время следующей проверки, а не дату встречи.")
    if action == "reschedule" and due_date is None:
        raise ValidationError("Укажите новую дату выполнения.")
    if action != "reschedule" and (due_date is not None or due_time is not None):
        raise ValidationError("Новый срок указывается только при переносе.")
    if action != "wait" and next_check_at is not None:
        raise ValidationError("Следующая проверка указывается только для ожидания.")

    seed = ServiceTask.objects.filter(pk=task_id).select_related("organization").first()
    if not seed or not has_access(seed, user, write=True):
        raise PermissionDenied
    task = locked_task_with_crm_graph(organization=seed.organization, task_id=task_id)
    if not task or not has_access(task, user, write=True):
        raise PermissionDenied
    # Early checks may be stale after waiting for task/CRM locks. Re-evaluate
    # the same edit policy from current locked user/role/participant rows.
    user = lock_feedback_actor(task, user)
    if task.task_type != ServiceTask.TYPE_CRM_FOLLOWUP:
        raise ValidationError("Эти действия предназначены для задач CRM-сопровождения.")

    request_key = "feedback:" + _digest({"user": user.pk, "request": request_id})[:32]
    command = {
        "action": action, "comment": comment, "due_date": _iso(due_date),
        "due_time": _iso(due_time), "next_check_at": _iso(next_check_at),
    }
    command_hash = _digest(command)
    existing = task.changes.filter(field_name=request_key).first()
    if existing:
        try:
            previous = json.loads(existing.new_value)
        except (TypeError, ValueError):
            raise FeedbackConflict("Повтор запроса не может быть безопасно подтверждён.")
        if previous.get("command_hash") != command_hash:
            raise FeedbackConflict("Этот запрос уже использован для другого изменения.")
        return task, existing, False
    if not isinstance(expected_version, str) or version_for(task) != expected_version:
        raise FeedbackConflict("Задача уже изменена. Обновите карточку и проверьте актуальные данные; ваш текст сохранён в форме.")
    if task.is_archived or task.completed_at or task.status in {ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED}:
        raise ValidationError("Задача уже завершена, отменена или архивирована.")

    before = snapshot(task)
    payload = dict(task.payload_json) if isinstance(task.payload_json, dict) else {}
    state = state_for(task)
    state["revision"] = int(state.get("revision", 0)) + 1
    state["last_action"] = action
    state["last_actor_id"] = user.pk
    state["last_changed_at"] = timezone.now().isoformat()
    # This records pending work, not a claim that DOT has read it.
    state["requires_review"] = True
    changed_fields = ["payload_json", "updated_at"]

    if action != "comment":
        state["control_revision"] = int(state.get("control_revision", 0)) + 1
        state["reason"] = comment
        payload.pop("control_state", None)
    if action == "wait":
        state["mode"] = "waiting"
        state["next_check_at"] = next_check_at.isoformat()
        task.status = ServiceTask.STATUS_WAITING
        zone = ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))
        changed_fields += schedule_waiting_check(task, state, next_check_at.astimezone(zone))
        payload["needs_due_date"] = True
        changed_fields.append("status")
    elif action == "reschedule":
        changed_fields += finish_waiting_check(task, state)
        state["mode"] = "active"
        state["next_check_at"] = None
        task.status = ServiceTask.STATUS_NEW
        task.start_date = task.end_date = due_date
        task.start_time = task.end_time = due_time
        task.due_at = _aware_local(datetime.combine(due_date, due_time)) if due_time else None
        payload["needs_due_date"] = False
        changed_fields += ["status", "start_date", "end_date", "start_time", "end_time", "due_at"]
    elif action in {"cancel", "complete"}:
        changed_fields += finish_waiting_check(task, state)
        state["mode"] = action
        state["next_check_at"] = None
        task.status = ServiceTask.STATUS_CANCELLED if action == "cancel" else ServiceTask.STATUS_DONE
        changed_fields += ["status"]

    payload[NAMESPACE] = state
    task.payload_json = payload
    task.save(update_fields=changed_fields)
    if action == "complete":
        archive_task(task, ServiceTask.ARCHIVE_REASON_COMPLETED, user)
        task.refresh_from_db()
    event = dict(command, schema=EVENT_SCHEMA, revision=state["revision"],
                 command_hash=command_hash, before=before,
                 after_status=task.status, task_id=task.pk, actor_id=user.pk)
    change = ServiceTaskChange.objects.create(
        task=task, changed_by=user,
        action=(ServiceTaskChange.ACTION_MOVED if action == "reschedule" else
                ServiceTaskChange.ACTION_COMPLETED if action == "complete" else ServiceTaskChange.ACTION_UPDATED),
        field_name=request_key,
        new_value=json.dumps(event, ensure_ascii=False, separators=(",", ":")),
    )
    return task, change, True
