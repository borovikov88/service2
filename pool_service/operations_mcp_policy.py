from django.conf import settings

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
