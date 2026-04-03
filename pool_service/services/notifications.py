from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse

from pool_service.models import ClientAccess, Notification, OrganizationAccess, OrganizationWaterNorms, WaterReading
from pool_service.services.push_notifications import send_push_to_users


READING_LABELS = {
    "ph": "pH",
    "cl_free": "\u0421\u0432\u043e\u0431\u043e\u0434\u043d\u044b\u0439 \u0445\u043b\u043e\u0440",
    "cl_total": "\u041e\u0431\u0449\u0438\u0439 \u0445\u043b\u043e\u0440",
}

READING_LABELS_GENITIVE = {
    "ph": "pH",
    "cl_free": "\u0441\u0432\u043e\u0431\u043e\u0434\u043d\u043e\u0433\u043e \u0445\u043b\u043e\u0440\u0430",
    "cl_total": "\u043e\u0431\u0449\u0435\u0433\u043e \u0445\u043b\u043e\u0440\u0430",
}


def _limits_for_org(organization):
    base = getattr(settings, "WATER_READING_LIMITS", {})
    if not organization:
        return base
    norms = OrganizationWaterNorms.objects.filter(organization=organization).first()
    if not norms:
        return base

    def _limit(field):
        min_value = getattr(norms, f"{field}_min", None)
        max_value = getattr(norms, f"{field}_max", None)
        base_limit = base.get(field, {})
        if min_value is None:
            min_value = base_limit.get("min")
        if max_value is None:
            max_value = base_limit.get("max")
        if min_value is None and max_value is None:
            return None
        return {"min": min_value, "max": max_value}

    limits = {}
    for field in READING_LABELS:
        limit = _limit(field)
        if limit:
            limits[field] = limit
    return limits


def _reading_violations(reading, limits):
    violations = []
    for field, label in READING_LABELS.items():
        value = getattr(reading, field, None)
        if value is None:
            continue
        limit = limits.get(field) if limits else None
        if not limit:
            continue
        min_value = limit.get("min")
        max_value = limit.get("max")
        if min_value is not None and value < min_value:
            violations.append(f"{label}: {value} < {min_value}")
        elif max_value is not None and value > max_value:
            violations.append(f"{label}: {value} > {max_value}")
    return violations


def _object_location_phrase(name):
    if not name:
        return "\u041d\u0430 \u043e\u0431\u044a\u0435\u043a\u0442\u0435"
    if name.startswith("\u0428\u043a\u043e\u043b\u0430 "):
        return f'\u0412 "{name.replace("\u0428\u043a\u043e\u043b\u0430 ", "\u0428\u043a\u043e\u043b\u0435 ", 1)}"'
    if name.startswith("\u0414\u0435\u0442\u0441\u043a\u0438\u0439 \u0441\u0430\u0434 "):
        return f'\u0412 "{name.replace("\u0414\u0435\u0442\u0441\u043a\u0438\u0439 \u0441\u0430\u0434 ", "\u0414\u0435\u0442\u0441\u043a\u043e\u043c \u0441\u0430\u0434\u0443 ", 1)}"'
    return f'\u041d\u0430 \u043e\u0431\u044a\u0435\u043a\u0442\u0435 "{name}"'


def _reading_violation_messages(reading, limits):
    messages = []
    for field in READING_LABELS:
        value = getattr(reading, field, None)
        if value is None:
            continue
        limit = limits.get(field) if limits else None
        if not limit:
            continue
        min_value = limit.get("min")
        max_value = limit.get("max")
        label = READING_LABELS_GENITIVE.get(field, READING_LABELS.get(field, field).lower())
        if min_value is not None and value < min_value:
            messages.append(f"\u043d\u0438\u0437\u043a\u0438\u0439 \u0443\u0440\u043e\u0432\u0435\u043d\u044c {label} - {value}")
        elif max_value is not None and value > max_value:
            messages.append(f"\u0432\u044b\u0441\u043e\u043a\u0438\u0439 \u0443\u0440\u043e\u0432\u0435\u043d\u044c {label} - {value}")
    return messages


def _create_notification(user, *, title, message, kind, level="info", action_url="", organization=None, client=None, pool=None, dedupe_key=""):
    payload = {
        "title": title,
        "message": message,
        "kind": kind,
        "level": level,
        "action_url": action_url,
        "organization": organization,
        "client": client,
        "pool": pool,
        "dedupe_key": dedupe_key or "",
    }
    if dedupe_key:
        obj, created = Notification.objects.get_or_create(
            user=user,
            dedupe_key=dedupe_key,
            defaults=payload,
        )
        return obj, created
    return Notification.objects.create(user=user, **payload), True


def notify_users(
    users,
    *,
    title,
    message,
    kind,
    level="info",
    action_url="",
    organization=None,
    client=None,
    pool=None,
    dedupe_key="",
    send_in_app=True,
    send_push=True,
):
    created = []
    for user in users:
        if not user or not user.is_active:
            continue
        obj = None
        was_created = False
        if send_in_app:
            obj, was_created = _create_notification(
                user,
                title=title,
                message=message,
                kind=kind,
                level=level,
                action_url=action_url,
                organization=organization,
                client=client,
                pool=pool,
                dedupe_key=dedupe_key,
            )
            if was_created:
                created.append(obj)
        if send_push:
            send_push_to_users(
                [user],
                title=title,
                message=message,
                action_url=action_url,
                notification=obj,
            )
    return created


def notify_superusers(*, title, message, kind, level="info", action_url="", send_in_app=True, send_push=True):
    users = User.objects.filter(is_superuser=True, is_active=True)
    return notify_users(users, title=title, message=message, kind=kind, level=level, action_url=action_url, send_in_app=send_in_app, send_push=send_push)


def notify_org_users(
    organization,
    *,
    title,
    message,
    kind,
    level="info",
    action_url="",
    pool=None,
    dedupe_key="",
    send_in_app=True,
    send_push=True,
):
    users = User.objects.filter(organizationaccess__organization=organization, is_active=True).distinct()
    return notify_users(
        users,
        title=title,
        message=message,
        kind=kind,
        level=level,
        action_url=action_url,
        organization=organization,
        pool=pool,
        dedupe_key=dedupe_key,
        send_in_app=send_in_app,
        send_push=send_push,
    )


def notify_client_users(
    client,
    *,
    title,
    message,
    kind,
    level="info",
    action_url="",
    pool=None,
    dedupe_key="",
    send_in_app=True,
    send_push=True,
):
    users = User.objects.filter(clientaccess__client=client, is_active=True).distinct()
    if client.user and client.user.is_active:
        users = list(users) + [client.user]
    return notify_users(
        users,
        title=title,
        message=message,
        kind=kind,
        level=level,
        action_url=action_url,
        client=client,
        pool=pool,
        dedupe_key=dedupe_key,
        send_in_app=send_in_app,
        send_push=send_push,
    )


def notify_reading_out_of_range(reading):
    if getattr(reading, "pk", None):
        reading = (
            WaterReading.objects.select_related("pool__client", "pool__organization", "added_by")
            .filter(pk=reading.pk)
            .first()
            or reading
        )
    pool = reading.pool
    if getattr(pool, "service_suspended", False):
        return []
    organization = pool.organization
    if not organization:
        return []
    is_service_staff = bool(
        reading.added_by_id
        and OrganizationAccess.objects.filter(
            user_id=reading.added_by_id,
            organization=organization,
        ).exists()
    )
    is_pool_staff = bool(
        reading.added_by_id
        and (
            (pool.client and pool.client.user_id == reading.added_by_id)
            or (pool.client and ClientAccess.objects.filter(user_id=reading.added_by_id, client=pool.client).exists())
        )
    )
    if is_service_staff:
        send_in_app = organization.notify_limits_service_staff
        send_push = organization.notify_limits_service_staff_push
    elif is_pool_staff:
        send_in_app = organization.notify_limits_pool_staff
        send_push = organization.notify_limits_pool_staff_push
    else:
        send_in_app = organization.notify_limits_service_staff
        send_push = organization.notify_limits_service_staff_push
    if not send_in_app and not send_push:
        return []
    limits = _limits_for_org(organization)
    violations = _reading_violations(reading, limits)
    if not violations:
        return []
    title = "\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u0435\u043b\u0438 \u0432\u043d\u0435 \u043d\u043e\u0440\u043c\u044b"
    client_label = pool.client.name if pool.client else pool.address
    human_violations = _reading_violation_messages(reading, limits)
    if human_violations:
        message = f"{_object_location_phrase(client_label)} " + "; ".join(human_violations)
    else:
        message = f"{client_label}: " + "; ".join(violations)
    action_url = reverse("pool_detail", kwargs={"pool_uuid": pool.uuid})
    dedupe_key = f"limits:{reading.uuid}"
    recipients = User.objects.filter(
        organizationaccess__organization=organization,
        is_active=True,
    ).distinct()
    if reading.added_by_id:
        recipients = recipients.exclude(id=reading.added_by_id)

    created = notify_users(
        recipients,
        title=title,
        message=message,
        kind="limits",
        level="warning",
        action_url=action_url,
        organization=organization,
        pool=pool,
        dedupe_key=dedupe_key,
        send_in_app=send_in_app,
        send_push=send_push,
    )
    return created


def notify_task_assignment(task, users, *, added_by=None, send_push=True):
    is_supply_request = task.task_type == getattr(task, "TYPE_SUPPLY_REQUEST", "supply_request")
    title = "\u041d\u043e\u0432\u0430\u044f \u0437\u0430\u044f\u0432\u043a\u0430" if is_supply_request else "\u041d\u043e\u0432\u0430\u044f \u0437\u0430\u0434\u0430\u0447\u0430"
    date_label = ""
    if task.start_date and task.end_date:
        if task.start_date == task.end_date:
            date_label = task.start_date.strftime("%d.%m.%Y")
        else:
            date_label = f"{task.start_date:%d.%m.%Y} \u2014 {task.end_date:%d.%m.%Y}"
    time_label = ""
    if task.start_time and task.end_time:
        time_label = f"{task.start_time:%H:%M} \u2014 {task.end_time:%H:%M}"
    elif task.start_time:
        time_label = f"{task.start_time:%H:%M}"
    elif task.end_time:
        time_label = f"{task.end_time:%H:%M}"

    details = " | ".join([part for part in [date_label, time_label] if part])
    if is_supply_request:
        object_name = ""
        if task.client_id and task.client:
            object_name = task.client.name
        elif task.pool_id and task.pool:
            object_name = task.pool.address
        materials = ""
        if isinstance(task.payload_json, dict):
            materials = (task.payload_json.get("required_materials") or "").strip()
        if not materials and task.water_reading_id and task.water_reading:
            materials = (task.water_reading.required_materials or "").strip()
        message = f"Заявка {object_name}: {materials}".strip(": ")
    else:
        message = task.title
        if details:
            message = f"{message} ({details})"

    action_url = reverse("task_edit", kwargs={"task_id": task.id})
    recipients = []
    for user in users:
        if not user or not user.is_active:
            continue
        if added_by and user.id == added_by.id:
            continue
        recipients.append(user)
    if not recipients:
        return []
    return notify_users(
        recipients,
        title=title,
        message=message,
        kind="task_assignment",
        level="info",
        action_url=action_url,
        organization=task.organization,
        send_push=send_push,
    )
