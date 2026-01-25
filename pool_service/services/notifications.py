from django.conf import settings
from django.contrib.auth.models import User
from django.urls import reverse

from pool_service.models import Notification, OrganizationAccess, OrganizationWaterNorms
from pool_service.services.push_notifications import send_push_to_users


READING_LABELS = {
    "ph": "pH",
    "cl_free": "\u0421\u0432\u043e\u0431\u043e\u0434\u043d\u044b\u0439 \u0445\u043b\u043e\u0440",
    "cl_total": "\u041e\u0431\u0449\u0438\u0439 \u0445\u043b\u043e\u0440",
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


def notify_users(users, *, title, message, kind, level="info", action_url="", organization=None, client=None, pool=None, dedupe_key=""):
    created = []
    for user in users:
        if not user or not user.is_active:
            continue
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
    return created


def notify_superusers(*, title, message, kind, level="info", action_url=""):
    users = User.objects.filter(is_superuser=True, is_active=True)
    return notify_users(users, title=title, message=message, kind=kind, level=level, action_url=action_url)


def notify_org_users(organization, *, title, message, kind, level="info", action_url="", pool=None, dedupe_key=""):
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
    )


def notify_client_users(client, *, title, message, kind, level="info", action_url="", pool=None, dedupe_key=""):
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
    )


def notify_reading_out_of_range(reading):
    pool = reading.pool
    if getattr(pool, "service_suspended", False):
        return []
    organization = pool.organization
    if not organization:
        return []
    if reading.added_by_id and OrganizationAccess.objects.filter(
        user_id=reading.added_by_id,
        organization=organization,
    ).exists():
        return []
    if not organization.notify_limits:
        return []
    limits = _limits_for_org(organization)
    violations = _reading_violations(reading, limits)
    if not violations:
        return []
    title = "\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u0435\u043b\u0438 \u0432\u043d\u0435 \u043d\u043e\u0440\u043c\u044b"
    client_label = pool.client.name if pool.client else pool.address
    message = f"{client_label}: " + "; ".join(violations)
    action_url = reverse("pool_detail", kwargs={"pool_uuid": pool.uuid})
    dedupe_key = f"limits:{reading.uuid}"
    created = notify_org_users(
        organization,
        title=title,
        message=message,
        kind="limits",
        level="warning",
        action_url=action_url,
        pool=pool,
        dedupe_key=dedupe_key,
    )
    is_org_staff = False
    if reading.added_by_id:
        is_org_staff = OrganizationAccess.objects.filter(
            user_id=reading.added_by_id,
            organization=organization,
        ).exists()
    if not is_org_staff:
        users = User.objects.filter(organizationaccess__organization=organization, is_active=True).distinct()
        send_push_to_users(users, title=title, message=message, action_url=action_url)
    return created
