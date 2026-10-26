"""V2 read policy and advisory write availability; commands reauthorize under locks."""
from django.contrib.auth import get_user_model
from django.db.models import Q, F

from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.crm_commercial_models import CrmAccess, CrmDeal


def can_view_team_deals(user, organization):
    if not user or not user.is_authenticated or not organization:
        return False
    if not get_user_model().objects.filter(pk=user.pk, is_active=True).exists():
        return False
    if not Organization.objects.filter(pk=organization.pk, crm_v2_read=True).exists():
        return False
    roles = set(OrganizationAccess.objects.filter(user_id=user.pk, organization_id=organization.pk).values_list("role", flat=True))
    if not roles:
        return False
    if Employee.objects.filter(organization_id=organization.pk, user_id=user.pk).filter(Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED)).exists():
        return False
    # D06 is the sole implicit role grant. Other roles need an explicit read grant.
    return "manager" in roles or CrmAccess.objects.filter(organization_id=organization.pk, user_id=user.pk, view_team_deals=True).exists()


def deals_for_user(user, organization):
    if not can_view_team_deals(user, organization):
        return CrmDeal.objects.none()
    return CrmDeal.objects.filter(organization_id=organization.pk).filter(Q(client__isnull=True) | Q(client__organization_id=F("organization_id")))


def can_write_commercial(user, organization):
    # Availability only: this does not authorize any particular card/action.
    # execute_commercial_command locks/rechecks all current rights and ownership.
    if not can_view_team_deals(user, organization):
        return False
    if not Organization.objects.filter(pk=organization.pk, crm_v2_write=True).exists():
        return False
    return CrmAccess.objects.filter(organization_id=organization.pk, user_id=user.pk).filter(
        Q(edit_own_leads=True) | Q(transition_own_leads=True)
        | Q(edit_own_deals=True) | Q(transition_own_deals=True)
    ).exists()
