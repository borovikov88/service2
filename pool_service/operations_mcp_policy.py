from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction

from pool_service.models import OrganizationAccess


ALLOWED_ROLES = frozenset({"owner", "admin"})


def can_access_operations_mcp(user, organization):
    if not user or not getattr(user, "is_authenticated", False) or not user.is_active:
        return False
    if not organization:
        return False
    if user.is_superuser:
        return bool(getattr(settings, "ADVISOR_OPERATIONS_MCP_ALLOW_SUPERUSER", False))
    return OrganizationAccess.objects.filter(
        user=user,
        organization=organization,
        role__in=ALLOWED_ROLES,
    ).exists()


def locked_operations_actor(actor_id, organization):
    """Revalidate authority after business locks, inside the write transaction.

    Only identity comes from the authenticated request. Never reuse cached
    activity, superuser or organization-role flags. Lock order matches human
    task feedback: business rows, user, then that user's organization roles.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Operations authorization requires a write transaction.")
    if not actor_id or not organization or not organization.pk:
        raise PermissionError("actor")
    actor = User.objects.select_for_update().filter(pk=actor_id).first()
    if actor is None or not actor.is_active:
        raise PermissionError("actor")
    roles = set(
        OrganizationAccess.objects.select_for_update()
        .filter(user_id=actor.pk, organization_id=organization.pk)
        .order_by("pk")
        .values_list("role", flat=True)
    )
    if actor.is_superuser:
        allowed = bool(getattr(settings, "ADVISOR_OPERATIONS_MCP_ALLOW_SUPERUSER", False))
    else:
        allowed = bool(roles & ALLOWED_ROLES)
    if not allowed:
        raise PermissionError("actor")
    return actor
