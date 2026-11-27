from django.contrib.auth import get_user_model
from django.db.models import F, Q
from django.utils import timezone

from pool_service.models import Employee, OrganizationAccess
from pool_service.staff_call_models import StaffAccount
from pool_service.staff_structure_models import DepartmentAssignment


def mailbox_role(user, organization):
    if not user or not user.is_authenticated or not organization:
        return ""
    if not get_user_model().objects.filter(pk=user.pk, is_active=True).exists():
        return ""
    memberships = set(
        OrganizationAccess.objects.filter(organization=organization, user=user).values_list("role", flat=True)
    )
    if (
        not memberships
        or Employee.objects.filter(organization=organization, user=user)
        .filter(Q(is_active=False) | Q(employment_status="dismissed"))
        .exists()
        or StaffAccount.objects.filter(
            organization=organization, user=user, kind__in=("system", "service")
        ).exists()
    ):
        return ""
    if "owner" in memberships:
        return "owner"
    if "manager" in memberships:
        return "manager"
    now = timezone.now()
    appointment = (
        DepartmentAssignment.objects.filter(
            department__organization=organization,
            employee__organization=organization,
            user_at_assignment=user,
            employee__user_id=F("user_at_assignment_id"),
            employee__is_active=True,
            valid_from__lte=now,
        )
        .filter(Q(valid_to__isnull=True) | Q(valid_to__gt=now))
        .order_by("-valid_from", "-pk")
        .first()
    )
    return "sales_head" if appointment and appointment.position == "sales_head" else ""
