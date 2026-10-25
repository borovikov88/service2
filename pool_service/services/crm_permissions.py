"""Organization-bound policy for the existing CRM (no user-instance cache)."""
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db.models import F, Q

from pool_service.models import CrmItem, Employee, OrganizationAccess, ServiceTask


CRM_STAFF_ROLES = {"owner", "admin", "manager", "service", "installer"}
CRM_COMMERCIAL_ROLES = {"owner", "admin", "manager"}


def crm_roles(user, organization):
    if not user or not user.is_authenticated or not user.is_active or not organization:
        return set()
    if user.is_superuser:
        return {"superuser"}
    if Employee.objects.filter(organization=organization, user=user).filter(
        Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED),
    ).exists():
        return set()
    return set(OrganizationAccess.objects.filter(
        user=user, organization=organization,
    ).values_list("role", flat=True))


def crm_directions(user, organization):
    roles = crm_roles(user, organization)
    if roles & (CRM_COMMERCIAL_ROLES | {"superuser"}):
        return {value for value, _label in CrmItem.DIRECTION_CHOICES}
    if roles & {"service", "installer"}:
        return {CrmItem.DIRECTION_SERVICE}
    return set()


def crm_items_for_user(user, organization):
    return CrmItem.objects.filter(
        organization=organization, direction__in=crm_directions(user, organization),
    )


def require_crm_graph_scope(item_ids):
    """Refuse reverse writes/cascades for any inconsistent legacy tenant link."""
    if ServiceTask.objects.filter(crm_item_id__in=item_ids).exclude(
        organization_id=F("crm_item__organization_id"),
    ).exists():
        raise PermissionDenied


def can_sync_task_crm(task, user=None):
    """Task authority does not grant authority over the linked commercial card."""
    if not task.crm_item_id:
        return False
    if task.crm_item.organization_id != task.organization_id:
        return False
    # Calls without an actor are existing trusted background synchronization.
    return user is None or task.crm_item.direction in crm_directions(user, task.organization)


def crm_staff_users(organization):
    """Assignable staff: an absent HR profile keeps the existing account contract."""
    if not organization:
        return User.objects.none()
    unavailable = Employee.objects.filter(organization=organization, user__isnull=False).filter(
        Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED),
    ).values("user_id")
    return User.objects.filter(
        is_active=True,
        organizationaccess__organization=organization,
        organizationaccess__role__in=CRM_STAFF_ROLES,
    ).exclude(pk__in=unavailable).distinct()
