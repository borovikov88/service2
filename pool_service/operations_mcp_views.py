from __future__ import annotations

import json
from datetime import date, datetime
from time import monotonic
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core import signing
from django.contrib.auth.models import User
from django.contrib.auth.views import redirect_to_login
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseNotFound, HttpResponseServerError, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from pool_service import onec_diagnostic_mcp_views as transport
from pool_service.communication_models import CallAnalysis
from pool_service.models import (
    Client,
    FinanceMcpAuditEvent,
    Notification,
    Organization,
    OrganizationAccess,
    Profile,
    ServiceTask,
    ServiceTaskChange,
)
from pool_service.operations_mcp_auth import (
    OPERATIONS_SCOPE,
    OperationsMcpConfigurationError,
    OperationsMcpOAuthError,
    authenticate_bearer_header,
    authorization_redirect_uri,
    exchange_token,
    is_enabled,
    issue_authorization_code,
    operations_mcp_origin_is_allowed,
    protected_resource_metadata,
    protected_resource_metadata_url,
    scoped_organizations,
    target_organization,
    validate_authorization_request,
)
from pool_service.operations_mcp_policy import ALLOWED_ROLES
from pool_service.services.crm_locking import locked_task_for_completion
from pool_service.services.notifications import (
    notify_users,
    task_assignment_notification_content,
)
from pool_service.services.push_notifications import send_push_to_users
from pool_service.services.task_archive import archive_task


OPERATIONAL_STAFF_ROLES = frozenset({"owner", "admin", "manager", "service", "installer"})


TOOL_NAMES = (
    "list_control_tasks",
    "get_call_analysis",
    "create_task",
    "reschedule_task",
    "complete_task",
    "send_employee_notification",
)


def _security_schemes():
    return [{"type": "oauth2", "scopes": [OPERATIONS_SCOPE]}]


def _tool(name, description, properties, *, required=(), read_only=False, destructive=False, idempotent=False):
    schemes = _security_schemes()
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "idempotentHint": idempotent,
            "openWorldHint": False,
        },
        "securitySchemes": schemes,
        "_meta": {"securitySchemes": _security_schemes()},
    }


def _tool_definitions():
    return [
        _tool(
            "list_control_tasks",
            "List active Service2 tasks in the authorized organization for execution control.",
            {
                "responsible_user_id": {"type": "integer", "minimum": 1},
                "status": {
                    "type": "string",
                    "enum": [
                        ServiceTask.STATUS_NEW,
                        ServiceTask.STATUS_IN_PROGRESS,
                        ServiceTask.STATUS_WAITING,
                    ],
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            read_only=True,
            idempotent=True,
        ),
        _tool(
            "get_call_analysis",
            "Read one completed call transcript/summary/facts from the authorized organization.",
            {"call_id": {"type": "integer", "minimum": 1}},
            required=("call_id",),
            read_only=True,
            idempotent=True,
        ),
        _tool(
            "create_task",
            "Create one private CRM follow-up task for an active Service2 employee.",
            {
                "idempotency_key": {"type": "string", "minLength": 1, "maxLength": 80},
                "title": {"type": "string", "minLength": 1, "maxLength": 255},
                "description": {"type": "string", "maxLength": 4000},
                "responsible_user_id": {"type": "integer", "minimum": 1},
                "due_date": {"type": "string", "format": "date"},
                "due_time": {"type": "string", "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$"},
                "client_id": {"type": "integer", "minimum": 1},
                "priority": {
                    "type": "string",
                    "enum": [
                        ServiceTask.PRIORITY_LOW,
                        ServiceTask.PRIORITY_NORMAL,
                        ServiceTask.PRIORITY_HIGH,
                    ],
                },
            },
            required=("idempotency_key", "title", "responsible_user_id", "due_date"),
            idempotent=True,
        ),
        _tool(
            "reschedule_task",
            "Move an active task deadline and record the reason in task history.",
            {
                "task_id": {"type": "integer", "minimum": 1},
                "due_date": {"type": "string", "format": "date"},
                "due_time": {"type": "string", "pattern": "^([01]\\d|2[0-3]):[0-5]\\d$"},
                "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            required=("task_id", "due_date", "reason"),
            idempotent=True,
        ),
        _tool(
            "complete_task",
            "Mark one active task completed and archive it as completed.",
            {
                "task_id": {"type": "integer", "minimum": 1},
                "comment": {"type": "string", "maxLength": 1000},
            },
            required=("task_id",),
            idempotent=True,
        ),
        _tool(
            "send_employee_notification",
            "Send one deduplicated Service2 notification to an active employee.",
            {
                "employee_user_id": {"type": "integer", "minimum": 1},
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "message": {"type": "string", "minLength": 1, "maxLength": 2000},
                "dedupe_key": {"type": "string", "minLength": 1, "maxLength": 80},
                "task_id": {"type": "integer", "minimum": 1},
            },
            required=("employee_user_id", "title", "message", "dedupe_key", "task_id"),
            idempotent=True,
        ),
    ]


def _reject_unknown(arguments, allowed):
    unknown = set(arguments) - set(allowed)
    if unknown:
        raise ValueError("unknown arguments")


def _as_int(value, field, *, minimum=1, maximum=None):
    if isinstance(value, bool):
        raise ValueError(field)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(field)
    if parsed < minimum or (maximum is not None and parsed > maximum):
        raise ValueError(field)
    return parsed


def _as_text(value, field, *, required=False, maximum=1000):
    if value is None:
        if required:
            raise ValueError(field)
        return ""
    if not isinstance(value, str):
        raise ValueError(field)
    text = value.strip()
    if required and not text:
        raise ValueError(field)
    if len(text) > maximum:
        raise ValueError(field)
    return text


def _as_date(value, field):
    if not isinstance(value, str):
        raise ValueError(field)
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(field)


def _as_time(value, field):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError(field)
    try:
        return datetime.strptime(value, "%H:%M").time()
    except ValueError:
        raise ValueError(field)


def _communication_zone():
    try:
        return ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _authorized_actor(authenticated):
    actor = authenticated.grant.authorized_by
    if not actor or not actor.is_active:
        raise PermissionError("actor")
    return actor


def _staff_user(organization, user_id):
    return (
        User.objects.filter(
            pk=user_id,
            is_active=True,
            organizationaccess__organization=organization,
            organizationaccess__role__in=OPERATIONAL_STAFF_ROLES,
        )
        .distinct()
        .first()
    )


def _task_for_org(organization, task_id, *, for_update=False):
    queryset = ServiceTask.objects
    if for_update:
        queryset = queryset.select_for_update()
    return (
        queryset.select_related("client", "pool", "primary_responsible")
        .prefetch_related("responsibles")
        .filter(pk=task_id, organization=organization)
        .first()
    )


ASSIGNMENT_DELIVERY_PAYLOAD_KEY = "operations_assignment_delivery"
EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY = "operations_employee_notification_deliveries"
PUSH_RETRY_BATCH_LIMIT = 100


def _assignment_notification_key(task_id):
    return f"operations_mcp:task:{int(task_id)}:assignment"


def _assignment_delivery(task):
    payload = dict(task.payload_json) if isinstance(task.payload_json, dict) else {}
    delivery = payload.get(ASSIGNMENT_DELIVERY_PAYLOAD_KEY)
    if not isinstance(delivery, dict):
        delivery = {}
    return payload, dict(delivery)


def _assignment_recipient_is_authorized(task, responsible):
    if not responsible or not responsible.is_active:
        return False
    has_operational_access = OrganizationAccess.objects.select_for_update().filter(
        user_id=responsible.id,
        organization=task.organization,
        role__in=OPERATIONAL_STAFF_ROLES,
    ).exists()
    if not has_operational_access:
        return False
    return bool(
        task.primary_responsible_id == responsible.id
        or task.responsibles.filter(id=responsible.id).exists()
    )


def _schedule_assignment_push(task_id, responsible_user_id):
    if not responsible_user_id:
        return
    transaction.on_commit(
        lambda: _retry_assignment_push(task_id, responsible_user_id),
        robust=True,
    )


def _retry_assignment_push(task_id, responsible_user_id):
    with transaction.atomic():
        task = (
            ServiceTask.objects.select_for_update()
            .select_related("organization", "client", "pool", "water_reading")
            .filter(pk=task_id)
            .first()
        )
        if not task:
            return 0

        payload, delivery = _assignment_delivery(task)
        if delivery.get("push_delivered_at"):
            return 0

        responsible = User.objects.filter(
            pk=responsible_user_id,
            is_active=True,
        ).first()
        if not _assignment_recipient_is_authorized(task, responsible):
            delivery["push_last_attempt_at"] = timezone.now().isoformat()
            delivery["push_delivery_result"] = "blocked_not_authorized"
            payload[ASSIGNMENT_DELIVERY_PAYLOAD_KEY] = delivery
            task.payload_json = payload
            task.save(update_fields=["payload_json", "updated_at"])
            return 0

        if not _profile_allows_push(responsible):
            delivery["push_last_attempt_at"] = timezone.now().isoformat()
            delivery["push_delivery_result"] = "blocked_push_disabled"
            payload[ASSIGNMENT_DELIVERY_PAYLOAD_KEY] = delivery
            task.payload_json = payload
            task.save(update_fields=["payload_json", "updated_at"])
            return 0

        added_by_id = delivery.get("added_by_user_id")
        if added_by_id and int(added_by_id) == responsible.id:
            delivery["push_delivered_at"] = timezone.now().isoformat()
            delivery["push_delivery_result"] = "skipped_self"
            payload[ASSIGNMENT_DELIVERY_PAYLOAD_KEY] = delivery
            task.payload_json = payload
            task.save(update_fields=["payload_json", "updated_at"])
            return 0

        title, message, action_url = task_assignment_notification_content(task)
        dedupe_key = delivery.get("notification_dedupe_key") or _assignment_notification_key(task.id)
        notification = Notification.objects.filter(
            user=responsible,
            dedupe_key=dedupe_key,
        ).first()
        sent = send_push_to_users(
            [responsible],
            title=title,
            message=message,
            action_url=action_url,
            notification=notification,
        )
        delivery["push_last_attempt_at"] = timezone.now().isoformat()
        delivery["push_last_sent_count"] = int(sent or 0)
        if sent:
            delivery["push_delivered_at"] = timezone.now().isoformat()
            delivery["push_delivery_result"] = "sent"
        else:
            delivery["push_delivery_result"] = "pending_retry"
        payload[ASSIGNMENT_DELIVERY_PAYLOAD_KEY] = delivery
        task.payload_json = payload
        task.save(update_fields=["payload_json", "updated_at"])
        return int(sent or 0)



def _profile_allows_push(user):
    profile = Profile.objects.filter(user=user).only("push_notifications_enabled").first()
    return True if profile is None else bool(profile.push_notifications_enabled)


def _notification_deliveries(task):
    payload = dict(task.payload_json) if isinstance(task.payload_json, dict) else {}
    deliveries = payload.get(EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY)
    if not isinstance(deliveries, dict):
        deliveries = {}
    return payload, dict(deliveries)


def _retry_employee_notification_push(task_id, marker):
    with transaction.atomic():
        task = (
            ServiceTask.objects.select_for_update()
            .select_related("organization", "primary_responsible")
            .prefetch_related("responsibles")
            .filter(pk=task_id, task_type=ServiceTask.TYPE_CRM_FOLLOWUP)
            .first()
        )
        if not task:
            return 0

        payload, deliveries = _notification_deliveries(task)
        delivery = deliveries.get(marker)
        if not isinstance(delivery, dict):
            return 0
        delivery = dict(delivery)
        if delivery.get("push_delivered_at"):
            return 0
        if delivery.get("push_delivery_result") in {
            "blocked_not_authorized",
            "blocked_push_disabled",
        }:
            return 0

        employee_id = delivery.get("employee_user_id")
        employee = User.objects.filter(pk=employee_id, is_active=True).first()
        if not employee or not _assignment_recipient_is_authorized(task, employee):
            delivery["push_last_attempt_at"] = timezone.now().isoformat()
            delivery["push_delivery_result"] = "blocked_not_authorized"
            deliveries[marker] = delivery
            payload[EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY] = deliveries
            task.payload_json = payload
            task.save(update_fields=["payload_json", "updated_at"])
            return 0

        if not _profile_allows_push(employee):
            delivery["push_last_attempt_at"] = timezone.now().isoformat()
            delivery["push_delivery_result"] = "blocked_push_disabled"
            deliveries[marker] = delivery
            payload[EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY] = deliveries
            task.payload_json = payload
            task.save(update_fields=["payload_json", "updated_at"])
            return 0

        notification = None
        notification_id = delivery.get("notification_id")
        if notification_id:
            notification = Notification.objects.filter(
                pk=notification_id,
                user=employee,
            ).first()

        sent = send_push_to_users(
            [employee],
            title=str(delivery.get("title") or ""),
            message=str(delivery.get("message") or ""),
            action_url=str(delivery.get("action_url") or ""),
            notification=notification,
        )
        delivery["push_last_attempt_at"] = timezone.now().isoformat()
        delivery["push_last_sent_count"] = int(sent or 0)
        if sent:
            delivery["push_delivered_at"] = timezone.now().isoformat()
            delivery["push_delivery_result"] = "sent"
        else:
            delivery["push_delivery_result"] = "pending_retry"
        deliveries[marker] = delivery
        payload[EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY] = deliveries
        task.payload_json = payload
        task.save(update_fields=["payload_json", "updated_at"])
        return int(sent or 0)


def _schedule_employee_notification_push(task_id, marker):
    transaction.on_commit(
        lambda: _retry_employee_notification_push(task_id, marker),
        robust=True,
    )


def process_pending_operations_pushes(*, limit=PUSH_RETRY_BATCH_LIMIT):
    """Retry durable Operations push deliveries left pending after commit/process failure."""
    limit = max(1, min(int(limit), 500))
    task_ids = list(
        ServiceTask.objects.filter(
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            payload_json__isnull=False,
        )
        .filter(
            Q(payload_json__has_key=ASSIGNMENT_DELIVERY_PAYLOAD_KEY)
            | Q(payload_json__has_key=EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY)
        )
        .order_by("updated_at", "id")
        .values_list("id", flat=True)[:limit]
    )

    result = {
        "checked": 0,
        "assignment_attempts": 0,
        "notification_attempts": 0,
        "delivered": 0,
    }

    for task_id in task_ids:
        task = ServiceTask.objects.filter(pk=task_id).only(
            "id",
            "payload_json",
            "primary_responsible_id",
        ).first()
        if not task:
            continue
        payload = task.payload_json if isinstance(task.payload_json, dict) else {}

        assignment = payload.get(ASSIGNMENT_DELIVERY_PAYLOAD_KEY)
        if isinstance(assignment, dict):
            result["checked"] += 1
            assignment_result = assignment.get("push_delivery_result")
            if (
                not assignment.get("push_delivered_at")
                and assignment_result not in {"blocked_not_authorized", "blocked_push_disabled", "skipped_self"}
            ):
                responsible_id = assignment.get("responsible_user_id") or task.primary_responsible_id
                if responsible_id:
                    result["assignment_attempts"] += 1
                    result["delivered"] += int(bool(_retry_assignment_push(task.id, responsible_id)))

        deliveries = payload.get(EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY)
        if isinstance(deliveries, dict):
            if not isinstance(assignment, dict):
                result["checked"] += 1
            for marker, delivery in list(deliveries.items()):
                if not isinstance(delivery, dict):
                    continue
                delivery_result = delivery.get("push_delivery_result")
                if (
                    delivery.get("push_delivered_at")
                    or delivery_result in {"blocked_not_authorized", "blocked_push_disabled"}
                ):
                    continue
                result["notification_attempts"] += 1
                result["delivered"] += int(
                    bool(_retry_employee_notification_push(task.id, marker))
                )

    return result


def _ensure_assignment_delivery(task, responsible, actor):
    dedupe_key = _assignment_notification_key(task.id)
    payload, delivery = _assignment_delivery(task)
    if not _assignment_recipient_is_authorized(task, responsible):
        Notification.objects.filter(
            user=responsible,
            dedupe_key=dedupe_key,
        ).delete()
        delivery["responsible_user_id"] = responsible.id if responsible else None
        delivery["added_by_user_id"] = actor.id if actor else None
        delivery["notification_dedupe_key"] = dedupe_key
        delivery["push_last_attempt_at"] = timezone.now().isoformat()
        delivery["push_delivery_result"] = "blocked_not_authorized"
        payload[ASSIGNMENT_DELIVERY_PAYLOAD_KEY] = delivery
        task.payload_json = payload
        task.save(update_fields=["payload_json", "updated_at"])
        return False

    notification = None
    if not actor or actor.id != responsible.id:
        title, message, action_url = task_assignment_notification_content(task)
        notification, _created = Notification.objects.get_or_create(
            user=responsible,
            dedupe_key=dedupe_key,
            defaults={
                "organization": task.organization,
                "kind": "task_assignment",
                "level": "info",
                "title": title,
                "message": message,
                "action_url": action_url,
            },
        )

    delivery["responsible_user_id"] = responsible.id
    delivery["added_by_user_id"] = actor.id if actor else None
    delivery["notification_dedupe_key"] = dedupe_key
    if notification:
        delivery["notification_id"] = notification.id
    payload[ASSIGNMENT_DELIVERY_PAYLOAD_KEY] = delivery
    task.payload_json = payload
    task.save(update_fields=["payload_json", "updated_at"])
    _schedule_assignment_push(task.id, responsible.id)
    return True


def _task_data(task):
    payload = task.payload_json if isinstance(task.payload_json, dict) else {}
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "priority": task.priority,
        "start_date": task.start_date.isoformat() if task.start_date else None,
        "end_date": task.end_date.isoformat() if task.end_date else None,
        "start_time": task.start_time.strftime("%H:%M") if task.start_time else None,
        "end_time": task.end_time.strftime("%H:%M") if task.end_time else None,
        "client_id": task.client_id,
        "client_name": task.client.name if task.client_id and task.client else None,
        "primary_responsible_id": task.primary_responsible_id,
        "primary_responsible": (
            task.primary_responsible.get_full_name() or task.primary_responsible.username
            if task.primary_responsible
            else None
        ),
        "source": payload.get("source"),
        "source_call_id": payload.get("source_call_id"),
        "auto_created": task.auto_created,
        "completed": bool(task.completed_at),
        "archived": task.is_archived,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
    }


def _list_control_tasks(organization, arguments):
    _reject_unknown(arguments, {"responsible_user_id", "status", "limit"})
    limit = _as_int(arguments.get("limit", 50), "limit", maximum=100)
    queryset = (
        ServiceTask.objects.filter(
            organization=organization,
            task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
            is_archived=False,
            completed_at__isnull=True,
        )
        .exclude(status__in=[ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED])
        .select_related("client", "primary_responsible")
        .order_by("end_date", "end_time", "id")
    )
    if arguments.get("responsible_user_id") is not None:
        responsible_id = _as_int(arguments["responsible_user_id"], "responsible_user_id")
        queryset = queryset.filter(primary_responsible_id=responsible_id)
    status = arguments.get("status")
    if status is not None:
        allowed = {ServiceTask.STATUS_NEW, ServiceTask.STATUS_IN_PROGRESS, ServiceTask.STATUS_WAITING}
        if status not in allowed:
            raise ValueError("status")
        queryset = queryset.filter(status=status)
    return {"tasks": [_task_data(task) for task in queryset[:limit]]}


def _get_call_analysis(organization, arguments):
    _reject_unknown(arguments, {"call_id"})
    call_id = _as_int(arguments.get("call_id"), "call_id")
    analysis = (
        CallAnalysis.objects.select_related("call", "call__employee", "call__client")
        .filter(call_id=call_id, call__organization=organization, status=CallAnalysis.STATUS_READY)
        .first()
    )
    if not analysis:
        raise ValueError("call_id")
    call = analysis.call
    return {
        "call_id": call.id,
        "started_at": call.started_at.isoformat(),
        "employee_user_id": call.employee_id,
        "client_id": call.client_id,
        "summary": analysis.summary,
        "transcript": analysis.transcript,
        "facts": analysis.facts if isinstance(analysis.facts, dict) else {},
    }


@transaction.atomic
def _create_task(authenticated, organization, arguments):
    _reject_unknown(
        arguments,
        {
            "idempotency_key",
            "title",
            "description",
            "responsible_user_id",
            "due_date",
            "due_time",
            "client_id",
            "priority",
        },
    )
    key = _as_text(arguments.get("idempotency_key"), "idempotency_key", required=True, maximum=80)
    # Serialize create requests per organization so concurrent MCP retries
    # cannot both pass the JSON idempotency lookup before either insert commits.
    organization = Organization.objects.select_for_update().get(pk=organization.pk)
    existing = (
        ServiceTask.objects.select_for_update()
        .select_related("primary_responsible", "created_by")
        .filter(
            organization=organization,
            payload_json__operations_mcp_idempotency_key=key,
        )
        .first()
    )
    if existing:
        if existing.primary_responsible:
            _ensure_assignment_delivery(
                existing,
                existing.primary_responsible,
                existing.created_by,
            )
        return {"created": False, "task": _task_data(existing)}

    title = _as_text(arguments.get("title"), "title", required=True, maximum=255)
    description = _as_text(arguments.get("description"), "description", maximum=4000)
    responsible_id = _as_int(arguments.get("responsible_user_id"), "responsible_user_id")
    responsible = _staff_user(organization, responsible_id)
    if not responsible:
        raise ValueError("responsible_user_id")
    due_date = _as_date(arguments.get("due_date"), "due_date")
    due_time = _as_time(arguments.get("due_time"), "due_time")
    priority = arguments.get("priority") or ServiceTask.PRIORITY_NORMAL
    if priority not in {ServiceTask.PRIORITY_LOW, ServiceTask.PRIORITY_NORMAL, ServiceTask.PRIORITY_HIGH}:
        raise ValueError("priority")

    client = None
    if arguments.get("client_id") is not None:
        client_id = _as_int(arguments["client_id"], "client_id")
        client = Client.objects.filter(pk=client_id, organization=organization).first()
        if not client:
            raise ValueError("client_id")

    actor = _authorized_actor(authenticated)
    due_at = None
    if due_time:
        due_at = datetime.combine(due_date, due_time).replace(tzinfo=_communication_zone())
    task = ServiceTask.objects.create(
        organization=organization,
        title=title,
        description=description,
        start_date=due_date,
        end_date=due_date,
        start_time=due_time,
        end_time=due_time,
        task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
        source_type=ServiceTask.SOURCE_MANAGER,
        status=ServiceTask.STATUS_NEW,
        visibility=ServiceTask.VISIBILITY_PRIVATE,
        priority=priority,
        client=client,
        created_by=actor,
        primary_responsible=responsible,
        auto_created=True,
        is_editable=True,
        due_at=due_at,
        payload_json={
            "source": "operations_mcp",
            "operations_mcp_idempotency_key": key,
            "operations_mcp_grant_id": authenticated.grant.id,
        },
    )
    task.responsibles.add(responsible)
    ServiceTaskChange.objects.create(
        task=task,
        changed_by=actor,
        action=ServiceTaskChange.ACTION_CREATED,
        new_value=task.title,
    )
    _ensure_assignment_delivery(task, responsible, actor)
    return {"created": True, "task": _task_data(task)}


@transaction.atomic
def _reschedule_task(authenticated, organization, arguments):
    _reject_unknown(arguments, {"task_id", "due_date", "due_time", "reason"})
    task_id = _as_int(arguments.get("task_id"), "task_id")
    due_date = _as_date(arguments.get("due_date"), "due_date")
    due_time = _as_time(arguments.get("due_time"), "due_time")
    reason = _as_text(arguments.get("reason"), "reason", required=True, maximum=500)
    task = _task_for_org(organization, task_id, for_update=True)
    if (
        not task
        or task.task_type != ServiceTask.TYPE_CRM_FOLLOWUP
        or task.is_archived
        or task.completed_at
        or task.status in {ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED}
    ):
        raise ValueError("task_id")
    actor = _authorized_actor(authenticated)
    old_date = task.end_date or task.start_date
    old_time = task.end_time or task.start_time
    old = f"{old_date} {old_time or ''}".strip()
    new_time = due_time if arguments.get("due_time") is not None else old_time
    if old_date == due_date and old_time == new_time:
        return {"changed": False, "task": _task_data(task)}
    task.start_date = due_date
    task.end_date = due_date
    task.start_time = new_time
    task.end_time = new_time
    task.due_at = (
        datetime.combine(due_date, new_time).replace(tzinfo=_communication_zone())
        if new_time
        else None
    )
    task.save(update_fields=["start_date", "end_date", "start_time", "end_time", "due_at", "updated_at"])
    ServiceTaskChange.objects.create(
        task=task,
        changed_by=actor,
        action=ServiceTaskChange.ACTION_MOVED,
        field_name="deadline",
        old_value=old,
        new_value=f"{due_date.isoformat()} {new_time.strftime('%H:%M') if new_time else ''} | {reason}".strip(),
    )
    return {"changed": True, "task": _task_data(task)}


@transaction.atomic
def _complete_task(authenticated, organization, arguments):
    _reject_unknown(arguments, {"task_id", "comment"})
    task_id = _as_int(arguments.get("task_id"), "task_id")
    comment = _as_text(arguments.get("comment"), "comment", maximum=1000)
    task = locked_task_for_completion(organization=organization, task_id=task_id)
    if not task or task.task_type != ServiceTask.TYPE_CRM_FOLLOWUP:
        raise ValueError("task_id")
    if task.is_completed_archive or task.completed_at or task.status == ServiceTask.STATUS_DONE:
        return {"completed": False, "task": _task_data(task)}
    if task.is_archived or task.status == ServiceTask.STATUS_CANCELLED:
        raise ValueError("task_id")
    actor = _authorized_actor(authenticated)
    task.status = ServiceTask.STATUS_DONE
    task.save(update_fields=["status", "updated_at"])
    archive_task(task, ServiceTask.ARCHIVE_REASON_COMPLETED, actor)
    ServiceTaskChange.objects.create(
        task=task,
        changed_by=actor,
        action=ServiceTaskChange.ACTION_COMPLETED,
        new_value=comment,
    )
    task.refresh_from_db()
    return {"completed": True, "task": _task_data(task)}


@transaction.atomic
def _send_employee_notification(authenticated, organization, arguments):
    _reject_unknown(arguments, {"employee_user_id", "title", "message", "dedupe_key", "task_id"})
    employee_id = _as_int(arguments.get("employee_user_id"), "employee_user_id")
    title = _as_text(arguments.get("title"), "title", required=True, maximum=200)
    message = _as_text(arguments.get("message"), "message", required=True, maximum=2000)
    key = _as_text(arguments.get("dedupe_key"), "dedupe_key", required=True, maximum=80)
    task_id = _as_int(arguments.get("task_id"), "task_id")

    task = (
        ServiceTask.objects.select_for_update()
        .select_related("primary_responsible")
        .prefetch_related("responsibles")
        .filter(pk=task_id, organization=organization, task_type=ServiceTask.TYPE_CRM_FOLLOWUP)
        .first()
    )
    if (
        not task
        or task.is_archived
        or task.completed_at
        or task.status in {ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED}
    ):
        raise ValueError("task_id")

    participant_ids = {user.id for user in task.responsibles.all()}
    if task.primary_responsible_id:
        participant_ids.add(task.primary_responsible_id)
    if employee_id not in participant_ids:
        raise ValueError("employee_user_id")
    employee = _staff_user(organization, employee_id)
    if not employee:
        raise ValueError("employee_user_id")

    payload, deliveries = _notification_deliveries(task)
    marker = f"{employee.id}:{key}"
    existing = deliveries.get(marker)
    if isinstance(existing, dict):
        if (
            not existing.get("push_delivered_at")
            and existing.get("push_delivery_result")
            not in {"blocked_not_authorized", "blocked_push_disabled"}
        ):
            _schedule_employee_notification_push(task.id, marker)
        return {"created_notifications": 0, "employee_user_id": employee.id}
    if len(deliveries) >= 50:
        raise ValueError("dedupe_key")

    action_url = reverse("task_edit", kwargs={"task_id": task.id})
    created = notify_users(
        [employee],
        title=title,
        message=message,
        kind="task_assignment",
        level="info",
        action_url=action_url,
        organization=organization,
        dedupe_key=f"operations_mcp:{task.id}:{key}",
        send_in_app=True,
        send_push=False,
    )
    notification = created[0] if created else None

    deliveries[marker] = {
        "employee_user_id": employee.id,
        "title": title,
        "message": message,
        "action_url": action_url,
        "notification_id": notification.id if notification else None,
        "notification_dedupe_key": f"operations_mcp:{task.id}:{key}",
        "push_delivery_result": (
            "pending_retry" if _profile_allows_push(employee) else "blocked_push_disabled"
        ),
    }
    payload[EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY] = deliveries
    task.payload_json = payload
    task.save(update_fields=["payload_json", "updated_at"])

    if deliveries[marker]["push_delivery_result"] == "pending_retry":
        _schedule_employee_notification_push(task.id, marker)

    return {"created_notifications": len(created), "employee_user_id": employee.id}


def _tool_dispatch(authenticated, name, arguments):
    organization = target_organization()
    if name == "list_control_tasks":
        return _list_control_tasks(organization, arguments)
    if name == "get_call_analysis":
        return _get_call_analysis(organization, arguments)
    if name == "create_task":
        return _create_task(authenticated, organization, arguments)
    if name == "reschedule_task":
        return _reschedule_task(authenticated, organization, arguments)
    if name == "complete_task":
        return _complete_task(authenticated, organization, arguments)
    if name == "send_employee_notification":
        return _send_employee_notification(authenticated, organization, arguments)
    raise ValueError("unknown tool")


def _audit(authenticated, name, *, result, started, response_bytes=0):
    organization = target_organization()
    FinanceMcpAuditEvent.objects.create(
        principal=authenticated.principal,
        grant=authenticated.grant,
        tool_name=f"operations.{str(name)[:88]}",
        organization_ids=[organization.id],
        result=result,
        duration_ms=max(0, int((monotonic() - started) * 1000)),
        response_bytes=max(0, int(response_bytes)),
    )


def _challenge(*, error=None, description=None):
    values = [
        f'resource_metadata="{protected_resource_metadata_url()}"',
        f'scope="{OPERATIONS_SCOPE}"',
        f'error="{error}"' if error else None,
        f'error_description="{description}"' if description else None,
    ]
    return "Bearer " + ", ".join(value for value in values if value)


def _tool_auth_required(request_id, *, protocol_version):
    return transport._mcp_response(
        transport._jsonrpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": "Authentication required before Service2 operational actions can run."}],
                "_meta": {"mcp/www_authenticate": [_challenge(error="insufficient_scope", description="Link Service2 Operations to continue")]},
                "isError": True,
            },
        ),
        protocol_version=protocol_version,
    )


def _http_auth_required(*, protocol_version):
    response = transport._mcp_response(
        transport._jsonrpc_error(None, -32001, "Operations MCP requires a valid Bearer token."),
        status=401,
        protocol_version=protocol_version,
    )
    response["WWW-Authenticate"] = _challenge()
    return response


@csrf_exempt
def operations_mcp(request):
    protocol_version = transport._protocol_version(request)
    if not is_enabled():
        return transport._no_store(HttpResponseNotFound())
    try:
        protected_resource_metadata()
        origin_allowed = operations_mcp_origin_is_allowed(request.headers.get("Origin"))
    except OperationsMcpConfigurationError:
        return transport._mcp_empty(status=503, protocol_version=protocol_version)
    if not origin_allowed:
        return transport._mcp_empty(status=403, protocol_version=protocol_version)

    if request.method == "OPTIONS":
        response = transport._mcp_empty(status=204, protocol_version=protocol_version)
        response["Allow"] = "POST, OPTIONS"
        return response
    if request.method != "POST":
        response = transport._mcp_empty(status=405, protocol_version=protocol_version)
        response["Allow"] = "POST, OPTIONS"
        return response

    authorization = request.headers.get("Authorization")
    authenticated = None
    if authorization:
        try:
            authenticated = authenticate_bearer_header(authorization)
        except OperationsMcpOAuthError as exc:
            response = transport._mcp_response(
                transport._jsonrpc_error(None, -32001, "Operations MCP authentication failed."),
                status=exc.status,
                protocol_version=protocol_version,
            )
            response["WWW-Authenticate"] = _challenge(error=exc.error, description=exc.description)
            return response

    payload, error_response = transport._require_json_mcp_request(request, protocol_version=protocol_version)
    if error_response is not None:
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return error_response

    request_id = payload.get("id")
    params = payload.get("params", {})
    if not isinstance(params, dict):
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return transport._mcp_response(
            transport._jsonrpc_error(request_id, -32602, "Invalid params"),
            protocol_version=protocol_version,
        )

    method = payload["method"]
    if method == "notifications/initialized":
        if "id" in payload:
            return transport._mcp_response(
                transport._jsonrpc_error(request_id, -32600, "notifications/initialized must not include an id"),
                status=400,
                protocol_version=protocol_version,
            )
        return transport._mcp_empty(status=202, protocol_version=protocol_version)

    if "id" not in payload:
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return transport._mcp_empty(status=202, protocol_version=protocol_version)

    if method == "initialize":
        requested_version = params.get("protocolVersion")
        client_info = params.get("clientInfo")
        if (
            not isinstance(requested_version, str)
            or not isinstance(params.get("capabilities"), dict)
            or not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not client_info.get("name")
            or not isinstance(client_info.get("version"), str)
            or not client_info.get("version")
        ):
            if not authorization:
                return _http_auth_required(protocol_version=protocol_version)
            return transport._mcp_response(
                transport._jsonrpc_error(request_id, -32602, "Invalid initialize params"),
                protocol_version=protocol_version,
            )
        selected = requested_version if requested_version in transport.SUPPORTED_PROTOCOL_VERSIONS else transport.MCP_PROTOCOL_VERSION
        return transport._mcp_response(
            transport._jsonrpc_result(
                request_id,
                {
                    "protocolVersion": selected,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "service2-operations", "version": "1.0.0"},
                    "instructions": "Controlled Service2 task and notification actions. Finance and 1C writes are not available.",
                },
            ),
            protocol_version=selected,
        )

    if request.headers.get("MCP-Protocol-Version") not in transport.SUPPORTED_PROTOCOL_VERSIONS:
        if not authorization:
            return _http_auth_required(protocol_version=protocol_version)
        return transport._mcp_response(
            transport._jsonrpc_error(request_id, -32600, "Unsupported MCP-Protocol-Version"),
            status=400,
            protocol_version=protocol_version,
        )

    if method == "tools/list":
        return transport._mcp_response(
            transport._jsonrpc_result(request_id, {"tools": _tool_definitions()}),
            protocol_version=protocol_version,
        )

    if method != "tools/call":
        return transport._mcp_response(
            transport._jsonrpc_error(request_id, -32601, "Method not found"),
            protocol_version=protocol_version,
        )
    if authenticated is None:
        return _tool_auth_required(request_id, protocol_version=protocol_version)

    name = params.get("name")
    arguments = params.get("arguments", {})
    started = monotonic()
    if not isinstance(name, str) or name not in TOOL_NAMES or not isinstance(arguments, dict):
        _audit(authenticated, name, result="denied", started=started)
        return transport._mcp_response(
            transport._jsonrpc_error(request_id, -32602, "Unknown tool or invalid arguments"),
            protocol_version=protocol_version,
        )

    try:
        with transaction.atomic():
            data = _tool_dispatch(authenticated, name, arguments)
            response_bytes = len(
                json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            _audit(
                authenticated,
                name,
                result="success",
                started=started,
                response_bytes=response_bytes,
            )
    except (ValueError, PermissionError):
        _audit(authenticated, name, result="denied", started=started)
        return transport._mcp_response(
            transport._jsonrpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": "Operations MCP rejected the request by server policy."}],
                    "isError": True,
                },
            ),
            protocol_version=protocol_version,
        )
    except Exception:
        _audit(authenticated, name, result="error", started=started)
        return transport._mcp_response(
            transport._jsonrpc_result(
                request_id,
                {
                    "content": [{"type": "text", "text": "Service2 could not safely perform the requested operation."}],
                    "isError": True,
                },
            ),
            protocol_version=protocol_version,
        )

    return transport._mcp_response(
        transport._jsonrpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": "Service2 operation completed."}],
                "structuredContent": data,
                "isError": False,
            },
        ),
        protocol_version=protocol_version,
    )


def operations_protected_resource_metadata(request):
    if request.method != "GET":
        response = HttpResponse(status=405)
        response["Allow"] = "GET"
        return transport._no_store(response)
    if not is_enabled():
        return transport._no_store(HttpResponseNotFound())
    try:
        return transport._no_store(JsonResponse(protected_resource_metadata()))
    except OperationsMcpConfigurationError:
        return transport._no_store(HttpResponseServerError())


def _single_value_params(query_dict):
    if any(len(values) != 1 for _key, values in query_dict.lists()):
        raise OperationsMcpOAuthError("invalid_request", "OAuth parameters must not repeat.")
    return {key: values[0] for key, values in query_dict.lists()}


def _oauth_error_page(_error):
    return transport._no_store(HttpResponseBadRequest("OAuth authorization request was rejected."))


_CONSENT_BINDING_SALT = "service2.operations-mcp.consent.v1"
_CONSENT_BINDING_MAX_AGE = 600


def _consent_binding(authorization):
    return signing.dumps(
        {
            "organization_id": authorization["organization_id"],
            "state": authorization["state"],
            "client_id": authorization["client"].client_id,
            "resource": authorization["resource"],
            "redirect_uri": authorization["redirect_uri"],
            "code_challenge": authorization["code_challenge"],
            "scopes": sorted(authorization["scopes"]),
        },
        salt=_CONSENT_BINDING_SALT,
        compress=True,
    )


def _validate_consent_binding(value, authorization):
    if not isinstance(value, str) or not value:
        return False
    try:
        bound = signing.loads(
            value,
            salt=_CONSENT_BINDING_SALT,
            max_age=_CONSENT_BINDING_MAX_AGE,
        )
    except signing.BadSignature:
        return False
    expected = {
        "organization_id": authorization["organization_id"],
        "state": authorization["state"],
        "client_id": authorization["client"].client_id,
        "resource": authorization["resource"],
        "redirect_uri": authorization["redirect_uri"],
        "code_challenge": authorization["code_challenge"],
        "scopes": sorted(authorization["scopes"]),
    }
    return bound == expected


@require_http_methods(["GET", "POST"])
def operations_oauth_authorize(request):
    if not is_enabled():
        return transport._no_store(HttpResponseNotFound())
    if not request.user.is_authenticated:
        return transport._no_store(redirect_to_login(request.get_full_path()))
    try:
        params = _single_value_params(request.GET if request.method == "GET" else request.POST)
        authorization = validate_authorization_request(params)
    except (OperationsMcpOAuthError, OperationsMcpConfigurationError) as exc:
        return _oauth_error_page(exc)

    client = authorization["client"]
    from pool_service.operations_mcp_auth import authorization_is_allowed

    if not authorization_is_allowed(request.user, client):
        return transport._no_store(
            redirect(
                authorization_redirect_uri(
                    authorization["redirect_uri"],
                    state=authorization["state"],
                    error="access_denied",
                )
            )
        )

    if request.method == "GET":
        return transport._no_store(
            render(
                request,
                "pool_service/operations_mcp/authorize.html",
                {
                    "client": client,
                    "organizations": scoped_organizations(client),
                    "scope": " ".join(authorization["scopes"]),
                    "oauth_params": params,
                    "consent_binding": _consent_binding(authorization),
                },
            )
        )

    if request.method == "POST" and not _validate_consent_binding(
        request.POST.get("consent_binding"),
        authorization,
    ):
        return transport._no_store(
            redirect(
                authorization_redirect_uri(
                    authorization["redirect_uri"],
                    state=authorization["state"],
                    error="access_denied",
                )
            )
        )

    if request.POST.get("decision") != "approve":
        return transport._no_store(
            redirect(
                authorization_redirect_uri(
                    authorization["redirect_uri"],
                    state=authorization["state"],
                    error="access_denied",
                )
            )
        )

    try:
        code = issue_authorization_code(authorization=authorization, user=request.user)
    except OperationsMcpOAuthError:
        return transport._no_store(
            redirect(
                authorization_redirect_uri(
                    authorization["redirect_uri"],
                    state=authorization["state"],
                    error="access_denied",
                )
            )
        )
    return transport._no_store(
        redirect(
            authorization_redirect_uri(
                authorization["redirect_uri"],
                code=code,
                state=authorization["state"],
            )
        )
    )


@csrf_exempt
@require_http_methods(["POST"])
def operations_oauth_token(request):
    if not is_enabled():
        return transport._no_store(HttpResponseNotFound())
    if request.content_type != "application/x-www-form-urlencoded":
        return transport._no_store(
            JsonResponse(
                {
                    "error": "invalid_request",
                    "error_description": "Content-Type must be application/x-www-form-urlencoded",
                },
                status=415,
            )
        )
    try:
        result = exchange_token(_single_value_params(request.POST))
    except OperationsMcpOAuthError as exc:
        response = JsonResponse(
            {"error": exc.error, "error_description": exc.description},
            status=exc.status,
        )
        if exc.status == 401:
            response["WWW-Authenticate"] = 'Basic realm="service2-operations"'
        return transport._no_store(response)
    except OperationsMcpConfigurationError:
        return transport._no_store(HttpResponseServerError())
    return transport._no_store(JsonResponse(result))


__all__ = [
    "TOOL_NAMES",
    "operations_mcp",
    "operations_oauth_authorize",
    "operations_oauth_token",
    "operations_protected_resource_metadata",
]
