"""Explicit owner-self setup for own-deal operations; no role-wide write grants."""
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q

from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import CrmAccess
from pool_service.models import DataAuditLog, Employee, Organization, OrganizationAccess
from pool_service.services.permissions import company_has_access


OWN_CAPABILITIES = (
    "view_team_deals", "create_own_deals", "edit_own_deals",
    "transition_own_deals", "manage_own_next_action",
)


def can_activate_pilot(user, organization):
    if not user or not user.is_authenticated or not organization:
        return False
    return (
        get_user_model().objects.filter(pk=user.pk, is_active=True).exists()
        and OrganizationAccess.objects.filter(user_id=user.pk, organization=organization, role="owner").exists()
        and not Employee.objects.filter(user_id=user.pk, organization=organization).filter(
            Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED)).exists()
    )


@transaction.atomic
def activate_owner_pilot(*, actor, organization_id):
    """Invoked only by an authenticated owner's explicit CSRF-protected POST."""
    organization = Organization.objects.select_for_update().filter(pk=organization_id).first()
    if not organization or not company_has_access(organization):
        raise PermissionDenied
    current_actor = get_user_model().objects.select_for_update().filter(pk=actor.pk).first()
    memberships = list(OrganizationAccess.objects.select_for_update().filter(
        user_id=actor.pk, organization=organization).order_by("pk"))
    employees = list(Employee.objects.select_for_update().filter(
        user_id=actor.pk, organization=organization).order_by("pk"))
    if (not current_actor or not current_actor.is_active
            or not any(membership.role == "owner" for membership in memberships)
            or any(not employee.is_active or employee.employment_status == Employee.STATUS_DISMISSED for employee in employees)):
        raise PermissionDenied
    grant, _created = CrmAccess.objects.select_for_update().get_or_create(
        organization=organization, user=current_actor)
    before = {field: getattr(grant, field) for field in OWN_CAPABILITIES}
    before.update({"crm_v2_read": organization.crm_v2_read, "crm_v2_write": organization.crm_v2_write})
    pipelines, stages = bootstrap_commercial_settings(organization.pk)
    for field in OWN_CAPABILITIES:
        setattr(grant, field, True)
    grant.save(update_fields=list(OWN_CAPABILITIES))
    organization.crm_v2_read = organization.crm_v2_write = True
    organization.save(update_fields=["crm_v2_read", "crm_v2_write"])
    after = {field: True for field in before}
    changed = [field for field in before if before[field] != after[field]]
    if changed or pipelines or stages:
        DataAuditLog.objects.create(
            entity_type="crm_pilot_activation", entity_id=str(grant.pk),
            action=DataAuditLog.ACTION_UPDATE, organization=organization, actor=current_actor,
            before=before, after=after, changed_fields=changed,
        )
    return organization
