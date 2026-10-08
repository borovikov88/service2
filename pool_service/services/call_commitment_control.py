from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from pool_service.models import OrganizationAccess, ServiceTask
from pool_service.services.notifications import notify_users
from pool_service.services.push_notifications import send_push_to_users
from pool_service.services.task_feedback import state_for, waiting_control


CONTROL_SOURCE = "call_analysis"
ACTOR_EMPLOYEE = "employee"
ACTOR_CLIENT = "client"
DATE_ONLY_DEADLINE_TIME = time(hour=18, minute=0)
NORMAL_ESCALATION_DELAY = timedelta(hours=24)
IMPORTANT_ESCALATION_DELAY = timedelta(hours=4)


def _communication_zone():
    zone_name = getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul")
    try:
        return ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _payload(task):
    return dict(task.payload_json) if isinstance(task.payload_json, dict) else {}


def _effective_deadline(task, payload):
    waiting, next_check = waiting_control(task)
    if waiting:
        return next_check
    # Current dates, not the original AI appointment, are authoritative.
    if task.end_date:
        due_time = task.end_time or task.start_time or DATE_ONLY_DEADLINE_TIME
        return datetime.combine(task.end_date, due_time).replace(tzinfo=_communication_zone())
    if payload.get("needs_due_date"):
        return None
    if task.due_at:
        if timezone.is_aware(task.due_at):
            return task.due_at.astimezone(_communication_zone())
        return task.due_at.replace(tzinfo=_communication_zone())
    if task.start_date:
        due_time = task.end_time or task.start_time or DATE_ONLY_DEADLINE_TIME
        return datetime.combine(task.start_date, due_time).replace(tzinfo=_communication_zone())
    return None


def _deadline_key(deadline):
    return deadline.strftime("%Y%m%d%H%M")


def _control_key(task, deadline):
    key = _deadline_key(deadline)
    generation = state_for(task).get("control_revision")
    return f"{key}:r{generation}" if generation else key


def _locked_users_with_access(task, user_ids, *, roles=None):
    """Re-read mutable recipient authority under row locks before delivery."""
    ordered_ids = list(dict.fromkeys(int(user_id) for user_id in user_ids if user_id))
    if not ordered_ids:
        return []

    locked_users = {
        user.id: user
        for user in User.objects.select_for_update()
        .filter(pk__in=ordered_ids, is_active=True)
        .order_by("id")
    }
    if not locked_users:
        return []

    access_qs = OrganizationAccess.objects.select_for_update().filter(
        organization_id=task.organization_id,
        user_id__in=locked_users,
    )
    if roles is not None:
        access_qs = access_qs.filter(role__in=roles)
    allowed = set(access_qs.values_list("user_id", flat=True))
    return [
        locked_users[user_id]
        for user_id in ordered_ids
        if user_id in locked_users and user_id in allowed
    ]


def _task_recipients(task):
    primary_id = task.primary_responsible_id
    if primary_id:
        # Preserve existing semantics: an active primary responsible is the
        # only recipient. If their org access was revoked, do not silently
        # reroute a private task to somebody else.
        primary = (
            User.objects.select_for_update()
            .filter(pk=primary_id)
            .first()
        )
        if primary and primary.is_active:
            allowed = OrganizationAccess.objects.select_for_update().filter(
                organization_id=task.organization_id,
                user_id=primary.id,
            ).exists()
            return [primary] if allowed else []

    participant_ids = list(
        task.responsibles.order_by("id").values_list("id", flat=True)
    )
    return _locked_users_with_access(task, participant_ids)


def _role_recipients(task, role):
    candidate_ids = list(
        OrganizationAccess.objects.filter(
            organization_id=task.organization_id,
            role=role,
        )
        .order_by("user_id")
        .values_list("user_id", flat=True)
    )
    return _locked_users_with_access(task, candidate_ids, roles={role})


def _owner_recipients(task):
    owners = _role_recipients(task, "owner")
    if owners:
        return owners
    return _role_recipients(task, "admin")


def _object_label(task):
    if task.client_id and task.client:
        return task.client.name
    if task.pool_id and task.pool:
        if task.pool.client_id and task.pool.client:
            return task.pool.client.name
        return task.pool.address
    return ""


def _responsible_label(task):
    user = task.primary_responsible
    return (user.get_full_name() or user.username) if user else "ответственный не назначен"


def _due_notification(task, actor):
    object_label = _object_label(task)
    suffix = f" — {object_label}" if object_label else ""
    if waiting_control(task)[0]:
        return (
            "Пора вернуться к согласованию",
            f"{task.title}{suffix}. Наступило время внутренней проверки. Уточните результат ожидания; дата выполнения пока не согласована.",
        )
    if actor == ACTOR_CLIENT:
        return (
            "Проверьте обещание клиента",
            f"{task.title}{suffix}. Срок ожидания наступил — проверьте результат и свяжитесь с клиентом при необходимости.",
        )
    return ("Срок задачи наступил", f"{task.title}{suffix}. Проверьте выполнение договорённости.")


def _escalation_notification(task, actor):
    label = _object_label(task)
    object_part = f" ({label})" if label else ""
    responsible = _responsible_label(task)
    if waiting_control(task)[0]:
        return (
            "Нужен результат внутренней проверки",
            f"{task.title}{object_part}. Время проверки прошло, результат ожидания не обновлён. Ответственный: {responsible}. Это не просрочка отменённой встречи.",
        )
    if actor == ACTOR_CLIENT:
        return (
            "Просрочено ожидание клиента",
            f"{task.title}{object_part}. Срок прошёл, задача всё ещё в ожидании. Ответственный: {responsible}.",
        )
    return (
        "Просрочена договорённость",
        f"{task.title}{object_part}. Задача не закрыта после срока. Ответственный: {responsible}.",
    )


def _eligible_tasks():
    return ServiceTask.objects.filter(
        task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
        is_archived=False, completed_at__isnull=True,
    ).exclude(status__in=[ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED]).filter(
        Q(auto_created=True, payload_json__source__in=[CONTROL_SOURCE, "operations_mcp"])
        | Q(payload_json__task_feedback__control_revision__gt=0)
    )


def _actor(task, payload):
    actor = str(payload.get("actor") or "").strip().lower()
    if actor in {ACTOR_EMPLOYEE, ACTOR_CLIENT}:
        return actor
    if payload.get("source") == "operations_mcp" or state_for(task).get("control_revision"):
        return ACTOR_EMPLOYEE
    return None


def _deliver_control_push(task_id, expected_key, *, escalation=False):
    # A user may change the task between the transaction and its callback.
    # Re-read current state under the shared lock; never emit the old reminder.
    with transaction.atomic():
        task = _eligible_tasks().select_for_update().select_related(
            "organization", "client", "pool", "pool__client", "primary_responsible",
        ).filter(pk=task_id).first()
        if task is None:
            return
        payload = _payload(task)
        deadline = _effective_deadline(task, payload)
        actor = _actor(task, payload)
        if deadline is None or not actor or _control_key(task, deadline) != expected_key:
            return
        recipients = _owner_recipients(task) if escalation else _task_recipients(task)
        if not recipients:
            return
        title, message = (_escalation_notification if escalation else _due_notification)(task, actor)
        send_push_to_users(recipients, title=title, message=message,
                           action_url=reverse("task_edit", kwargs={"task_id": task.id}))


def process_call_commitment_controls(*, now=None):
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    now_local = now.astimezone(_communication_zone())
    candidate_ids = list(_eligible_tasks().order_by("id").values_list("id", flat=True))
    result = {"checked": 0, "due_reminders": 0, "escalations": 0, "without_deadline": 0}
    for task_id in candidate_ids:
        with transaction.atomic():
            task = _eligible_tasks().select_for_update().select_related(
                "organization", "client", "pool", "pool__client", "primary_responsible",
            ).prefetch_related("responsibles").filter(pk=task_id).first()
            if task is None:
                continue
            payload = _payload(task)
            actor = _actor(task, payload)
            if not actor:
                continue
            result["checked"] += 1
            deadline = _effective_deadline(task, payload)
            if deadline is None:
                result["without_deadline"] += 1
                continue
            if now_local < deadline:
                continue
            key = _control_key(task, deadline)
            state = payload.get("control_state")
            if not isinstance(state, dict) or state.get("deadline_key") != key:
                state = {"deadline_key": key}
            changed = False
            action_url = reverse("task_edit", kwargs={"task_id": task.id})
            if not state.get("due_reminder_sent_at"):
                recipients = _task_recipients(task)
                if recipients:
                    title, message = _due_notification(task, actor)
                    notify_users(
                        recipients, title=title, message=message, kind="task_assignment",
                        level="warning", action_url=action_url, organization=task.organization,
                        client=task.client, dedupe_key=f"call_control:{task.id}:due:{key}",
                        send_in_app=True, send_push=False,
                    )
                    transaction.on_commit(lambda pk=task.id, expected=key: _deliver_control_push(pk, expected), robust=True)
                    state["due_reminder_sent_at"] = now.isoformat()
                    result["due_reminders"] += 1
                    changed = True
            delay = IMPORTANT_ESCALATION_DELAY if task.priority == ServiceTask.PRIORITY_HIGH else NORMAL_ESCALATION_DELAY
            if now_local >= deadline + delay and not state.get("escalated_at"):
                owners = _owner_recipients(task)
                if owners:
                    title, message = _escalation_notification(task, actor)
                    notify_users(
                        owners, title=title, message=message, kind="task_assignment",
                        level="critical", action_url=action_url, organization=task.organization,
                        client=task.client, dedupe_key=f"call_control:{task.id}:escalation:{key}",
                        send_in_app=True, send_push=False,
                    )
                    transaction.on_commit(lambda pk=task.id, expected=key: _deliver_control_push(pk, expected, escalation=True), robust=True)
                    state["escalated_at"] = now.isoformat()
                    result["escalations"] += 1
                    changed = True
            if changed:
                payload["control_state"] = state
                task.payload_json = payload
                task.save(update_fields=["payload_json", "updated_at"])
    return result
