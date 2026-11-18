"""Directory editing is separate from import, merging and identity administration."""
from django.contrib.auth import get_user_model
from django.db.models import Q

from pool_service.client_queries import active_clients
from pool_service.models import Employee, OrganizationAccess
from pool_service.staff_call_models import StaffAccount
from .permissions import company_has_access


def contact_edit_roles(user, client, *, lock=False):
    if not user.is_authenticated or not client.organization_id or not company_has_access(client.organization):
        return set()
    def current(queryset):
        return queryset.select_for_update() if lock else queryset
    if not current(get_user_model().objects).filter(pk=user.pk, is_active=True, is_superuser=False).exists():
        return set()
    roles = set(current(OrganizationAccess.objects).filter(user_id=user.pk, organization_id=client.organization_id,
        role__in=("owner", "admin", "manager")).values_list("role", flat=True))
    if not roles or not active_clients().filter(pk=client.pk, client_type__in=("private", "legal")).exists():
        return set()
    if current(Employee.objects).filter(user_id=user.pk, organization_id=client.organization_id).filter(
            Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED)).exists():
        return set()
    if current(StaffAccount.objects).filter(user_id=user.pk, organization_id=client.organization_id,
            kind__in=("service", "system")).exists():
        return set()
    return roles
