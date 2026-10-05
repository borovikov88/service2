from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone

from pool_service.models import OrganizationAccess, ServiceTask
from pool_service.services.notifications import notify_users


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
    # Current task dates are authoritative so manual rescheduling immediately
    # starts a new control cycle. due_at may contain the original AI deadline.
    if task.end_date:
        due_date = task.end_date
        due_time = task.end_time or task.start_time or DATE_ONLY_DEADLINE_TIME
        return datetime.combine(due_date, due_time).replace(tzinfo=_communication_zone())

    if payload.get("needs_due_date"):
        return None

    if task.due_at:
        due_at = task.due_at
        if timezone.is_aware(due_at):
            return due_at.astimezone(_communication_zone())
        return due_at.replace(tzinfo=_communication_zone())

    if task.start_date:
        due_time = task.end_time or task.start_time or DATE_ONLY_DEADLINE_TIME
        return datetime.combine(task.start_date, due_time).replace(tzinfo=_communication_zone())
    return None


def _deadline_key(deadline):
    return deadline.strftime("%Y%m%d%H%M")


def _task_recipients(task):
    if task.primary_responsible and task.primary_responsible.is_active:
        return [task.primary_responsible]
    return list(task.responsibles.filter(is_active=True).order_by("id"))


def _owner_recipients(task):
    owners = list(
        User.objects.filter(
            is_active=True,
            organizationaccess__organization=task.organization,
            organizationaccess__role="owner",
        )
        .distinct()
        .order_by("id")
    )
    if owners:
        return owners
    return list(
        User.objects.filter(
            is_active=True,
            organizationaccess__organization=task.organization,
            organizationaccess__role="admin",
        )
        .distinct()
        .order_by("id")
    )


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
    if not user:
        return "ответственный не назначен"
    return user.get_full_name() or user.username


def _due_notification(task, actor):
    object_label = _object_label(task)
    suffix = f" — {object_label}" if object_label else ""
    if actor == ACTOR_CLIENT:
        return (
            "Проверьте обещание клиента",
            f"{task.title}{suffix}. Срок ожидания наступил — проверьте результат и свяжитесь с клиентом при необходимости.",
        )
    return (
        "Срок задачи наступил",
        f"{task.title}{suffix}. Проверьте выполнение договорённости.",
    )


def _escalation_notification(task, actor):
    object_label = _object_label(task)
    object_part = f" ({object_label})" if object_label else ""
    responsible = _responsible_label(task)
    if actor == ACTOR_CLIENT:
        return (
            "Просрочено ожидание клиента",
            f"{task.title}{object_part}. Срок прошёл, задача всё ещё в ожидании. Ответственный: {responsible}.",
        )
    return (
        "Просрочена договорённость",
        f"{task.title}{object_part}. Задача не закрыта после срока. Ответственный: {responsible}.",
    )


def process_call_commitment_controls(*, now=None):
    now = now or timezone.now()
    if timezone.is_naive(now):
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    now_local = now.astimezone(_communication_zone())

    tasks = (
        ServiceTask.objects.filter(
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            source_type=ServiceTask.SOURCE_SYSTEM,
            auto_created=True,
            is_archived=False,
            completed_at__isnull=True,
        )
        .exclude(status=ServiceTask.STATUS_CANCELLED)
        .select_related(
            "organization",
            "client",
            "pool",
            "pool__client",
            "primary_responsible",
        )
        .prefetch_related("responsibles")
        .order_by("id")
    )

    result = {
        "checked": 0,
        "due_reminders": 0,
        "escalations": 0,
        "without_deadline": 0,
    }

    for task in tasks:
        payload = _payload(task)
        if payload.get("source") != CONTROL_SOURCE:
            continue
        actor = str(payload.get("actor") or "").strip().lower()
        if actor not in {ACTOR_EMPLOYEE, ACTOR_CLIENT}:
            continue

        result["checked"] += 1
        deadline = _effective_deadline(task, payload)
        if deadline is None:
            result["without_deadline"] += 1
            continue
        if now_local < deadline:
            continue

        deadline_key = _deadline_key(deadline)
        state = payload.get("control_state")
        if not isinstance(state, dict) or state.get("deadline_key") != deadline_key:
            state = {"deadline_key": deadline_key}

        changed = False
        action_url = reverse("task_edit", kwargs={"task_id": task.id})

        if not state.get("due_reminder_sent_at"):
            recipients = _task_recipients(task)
            if recipients:
                title, message = _due_notification(task, actor)
                notify_users(
                    recipients,
                    title=title,
                    message=message,
                    kind="task_assignment",
                    level="warning",
                    action_url=action_url,
                    organization=task.organization,
                    client=task.client,
                    dedupe_key=f"call_control:{task.id}:due:{deadline_key}",
                    send_in_app=True,
                    send_push=True,
                )
                state["due_reminder_sent_at"] = now.isoformat()
                result["due_reminders"] += 1
                changed = True

        escalation_delay = (
            IMPORTANT_ESCALATION_DELAY
            if task.priority == ServiceTask.PRIORITY_HIGH
            else NORMAL_ESCALATION_DELAY
        )
        if now_local >= deadline + escalation_delay and not state.get("escalated_at"):
            owners = _owner_recipients(task)
            if owners:
                title, message = _escalation_notification(task, actor)
                notify_users(
                    owners,
                    title=title,
                    message=message,
                    kind="task_assignment",
                    level="critical",
                    action_url=action_url,
                    organization=task.organization,
                    client=task.client,
                    dedupe_key=f"call_control:{task.id}:escalation:{deadline_key}",
                    send_in_app=True,
                    send_push=True,
                )
                state["escalated_at"] = now.isoformat()
                result["escalations"] += 1
                changed = True

        if changed:
            payload["control_state"] = state
            task.payload_json = payload
            task.save(update_fields=["payload_json", "updated_at"])

    return result
