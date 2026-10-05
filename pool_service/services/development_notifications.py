from django.db import transaction
from django.urls import reverse

from pool_service.models import Notification, OrganizationAccess, Profile
from pool_service.services.push_notifications import send_push_to_users


ANALYSIS_READY = "analysis_ready"
READY_FOR_DEPLOY = "ready_for_deploy"
HUMAN_REQUIRED = "human_required"
DEVELOPMENT_ROLES = {"owner", "admin"}


def _recipients(task):
    eligible_ids = set(
        OrganizationAccess.objects.filter(
            organization=task.organization,
            role__in=DEVELOPMENT_ROLES,
            user__is_active=True,
        ).values_list("user_id", flat=True)
    )
    if task.initiator_id in eligible_ids:
        return [task.initiator]
    return [access.user for access in (
        OrganizationAccess.objects.filter(
            organization=task.organization,
            role__in=DEVELOPMENT_ROLES,
            user__is_active=True,
        )
        .select_related("user")
        .order_by("user_id")
    )]


def _notify(task, *, title, message, dedupe_suffix):
    action_url = reverse("development_task_detail", args=[task.pk])
    created = []
    for user in _recipients(task):
        profile = Profile.objects.filter(user=user).first()
        in_app_enabled = profile is None or profile.in_app_notifications_enabled
        push_enabled = profile is None or profile.push_notifications_enabled
        if not in_app_enabled and not push_enabled:
            continue

        notification = None
        was_created = False
        if in_app_enabled:
            notification, was_created = Notification.objects.get_or_create(
                user=user,
                dedupe_key=f"development-task:{task.pk}:{dedupe_suffix}",
                defaults={
                    "organization": task.organization,
                    "kind": "development",
                    "level": "info",
                    "title": title,
                    "message": message,
                    "action_url": action_url,
                },
            )
            if was_created:
                created.append(notification)

        if push_enabled and (was_created or not in_app_enabled):
            transaction.on_commit(
                lambda user=user, notification=notification: send_push_to_users(
                    [user],
                    title=title,
                    message=message,
                    action_url=action_url,
                    notification=notification,
                )
            )
    return created


def notify_analysis_ready(task, iteration):
    return _notify(
        task,
        title=f"{task.reference}: анализ завершён",
        message=f"Задача «{task.title}» готова к отправке в Codex.",
        dedupe_suffix=f"analysis-ready:{iteration.pk}",
    )


def notify_ready_for_deploy(task):
    return _notify(
        task,
        title=f"{task.reference} готова к деплою",
        message=f"Задача «{task.title}» готова к production deployment.",
        dedupe_suffix="ready-for-deploy",
    )


def notify_human_required(task, reason, *, dedupe_suffix=HUMAN_REQUIRED):
    return _notify(
        task,
        title=f"{task.reference}: требуется решение",
        message=f"Задача «{task.title}» остановлена: {reason}",
        dedupe_suffix=dedupe_suffix,
    )
