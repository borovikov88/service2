"""Current task read authority for derived CRM surfaces.

Keep the existing direct-task alternatives: author, M2M participant, org
owner/admin, technical superuser. Public visibility and primary-responsible
alone have never granted direct-task access and do not grant it here.
Mutation/feedback locking checks remain separate.
"""
from urllib.parse import urlsplit

from django.contrib.auth.models import User
from django.db.models import F, Q
from django.urls import Resolver404, resolve

from pool_service.models import Client, Employee, OrganizationAccess, ServiceTask
from pool_service.services.call_privacy import private_source_task_ids


def readable_task_queryset(queryset, user, *, allow_preview=False):
    """Apply current account, organization and direct-task actor authority in SQL."""
    if not user or not user.is_authenticated:
        return queryset.none()
    actor = User.objects.filter(pk=user.pk, is_active=True).values("is_superuser").first()
    if actor is None:
        return queryset.none()
    if actor["is_superuser"]:
        return queryset
    unavailable_orgs = Employee.objects.filter(user_id=user.pk).filter(
        Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED)
    ).values("organization_id")
    memberships = OrganizationAccess.objects.filter(user_id=user.pk).exclude(
        organization_id__in=unavailable_orgs
    )
    authority = (
        Q(created_by_id=user.pk)
        | Q(responsibles__id=user.pk)
        | Q(organization_id__in=memberships.filter(role__in=("owner", "admin")).values("organization_id"))
    )
    if allow_preview:
        # Existing client-card/list preview; never used for direct task reads.
        authority |= Q(visibility=ServiceTask.VISIBILITY_PUBLIC) | Q(primary_responsible_id=user.pk)
    client_ids = Client.objects.filter(
        Q(user_id=user.pk) | Q(staff_accesses__user_id=user.pk)
    ).values("pk")
    organization_scope = (
        Q(organization_id__in=memberships.values("organization_id"))
        | Q(client_id__in=client_ids, client__organization_id=F("organization_id"))
        | Q(pool__client_id__in=client_ids, pool__client__organization_id=F("organization_id"))
    )
    return queryset.filter(organization_scope).filter(authority).distinct()


def readable_tasks(queryset, user, *, allow_preview=False):
    """Batch source privacy after SQL authority, without a query per task."""
    tasks = list(readable_task_queryset(queryset, user, allow_preview=allow_preview))
    hidden = private_source_task_ids(tasks)
    return [task for task in tasks if task.pk not in hidden]


def visible_task_notifications(notifications, user):
    """Recheck historical task messages using their existing protected URL.

    Notifications predate a task FK. Resolve only local task URLs, including
    query strings; a deleted task never leaves its old private message visible.
    Other notification types keep their existing behavior.
    """
    from pool_service.task_waiting_guards import guarded_task_edit

    notifications = list(notifications)
    references = {}
    for note in notifications:
        url = urlsplit(note.action_url or "")
        if url.scheme or url.netloc:
            continue
        try:
            match = resolve(url.path)
        except Resolver404:
            continue
        if (match.url_name in {"task_edit", "task_feedback"} or match.func == guarded_task_edit) and "task_id" in match.kwargs:
            references[note.pk] = match.kwargs["task_id"]
    if not references:
        return notifications
    tasks = readable_tasks(ServiceTask.objects.filter(pk__in=set(references.values())), user)
    allowed = {task.pk: task.organization_id for task in tasks}
    return [
        note for note in notifications
        if note.pk not in references
        or (
            references[note.pk] in allowed
            and (not note.organization_id or note.organization_id == allowed[references[note.pk]])
        )
    ]
