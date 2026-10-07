"""Re-evaluate task-feedback authority from locked, current database rows.

The caller already holds the task/CRM graph locks. Keep the same task edit
policy as views._task_can_edit, but do not reuse a request's cached user or an
unlocked organization-role read. In particular, membership alone is not admin
authority. No new roles or capabilities are granted here.
"""

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db import transaction

from pool_service.models import OrganizationAccess


def lock_feedback_actor(task, user):
    """Return the current authorized actor, with authority locked until commit."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Task feedback authorization requires an atomic transaction.")
    if not user or not getattr(user, "pk", None) or not task.organization_id:
        raise PermissionDenied

    actor = (
        User.objects.select_for_update()
        .filter(pk=user.pk)
        .first()
    )
    if not actor or not actor.is_active:
        raise PermissionDenied

    roles = set(
        OrganizationAccess.objects.select_for_update()
        .filter(organization_id=task.organization_id, user_id=actor.pk)
        .order_by("pk")
        .values_list("role", flat=True)
    )
    if not roles:
        raise PermissionDenied

    # These are precisely the existing edit-policy alternatives. The user row
    # and role rows above are current locking reads, including on MySQL. A
    # locking participant read avoids an older repeatable-read snapshot too.
    if (
        actor.is_superuser
        or task.created_by_id == actor.pk
        or roles.intersection({"owner", "admin"})
        or task.responsibles.select_for_update().filter(pk=actor.pk).exists()
    ):
        return actor
    raise PermissionDenied
