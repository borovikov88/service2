"""One deployment approval, scoped to one organization and current managers.

This is an operator service, never an authenticated-user activation policy.
The durable audit marker deliberately prevents grants from being re-applied.
"""
from django.contrib.auth import get_user_model
from django.db import transaction

from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.crm_commercial_models import CrmAccess
from pool_service.models import DataAuditLog, Employee, Organization, OrganizationAccess
from pool_service.services.crm_commercial_access import can_view_team_deals, can_write_commercial
from pool_service.services.crm_pilot_activation import OWN_CAPABILITIES
from pool_service.services.permissions import company_has_access


APPROVAL_ID = "crm-aqualine-managers-20261026"
ORGANIZATION_ID = 1
REQUESTER_ID = 5
AUDIT_ENTITY = "crm_approved_access"


class ProvisioningBlocked(Exception):
    """Contains only a fixed public error code, never database exception text."""


def _available(user, employees):
    return bool(user and user.is_active and not any(
        not employee.is_active or employee.employment_status == Employee.STATUS_DISMISSED
        for employee in employees if employee.user_id == user.pk
    ))


def _access_verified(user, organization, grants):
    grant = grants.get(user.pk) if user else None
    return bool(company_has_access(organization) and grant and all(getattr(grant, field) for field in OWN_CAPABILITIES)
                and can_view_team_deals(user, organization)
                and can_write_commercial(user, organization))


def provision(*, expected_sha, run_id=0, run_attempt=0, apply=False):
    # Discovery precedes the transaction: no stale MySQL repeatable-read snapshot
    # may precede the current locking policy reads. It is only a candidate set.
    candidate_ids = set(OrganizationAccess.objects.filter(
        organization_id=ORGANIZATION_ID).values_list("user_id", flat=True))
    candidate_ids.add(REQUESTER_ID)
    with transaction.atomic():
        organization = Organization.objects.select_for_update().filter(pk=ORGANIZATION_ID).first()
        if organization is None:
            raise ProvisioningBlocked("crm_access_organization_unavailable")
        marker = DataAuditLog.objects.select_for_update().filter(
            organization=organization, entity_type=AUDIT_ENTITY, entity_id=APPROVAL_ID).first()
        users = {user.pk: user for user in get_user_model().objects.select_for_update().filter(
            pk__in=candidate_ids).order_by("pk")}
        memberships = list(OrganizationAccess.objects.select_for_update().filter(
            organization=organization).order_by("pk"))
        manager_ids = {member.user_id for member in memberships if member.role == "manager"}
        # New membership after discovery is never granted without locking its user.
        # Abort the entire invocation, so the protected deployment can retry safely.
        if manager_ids - users.keys():
            raise ProvisioningBlocked("crm_access_membership_changed")
        employees = list(Employee.objects.select_for_update().filter(
            organization=organization, user_id__in=users).order_by("pk"))
        grants = {grant.user_id: grant for grant in CrmAccess.objects.select_for_update().filter(
            organization=organization, user_id__in=users).order_by("pk")}
        requester = users.get(REQUESTER_ID)
        requester_eligible = _available(requester, employees) and any(
            member.user_id == REQUESTER_ID and member.role == "admin" for member in memberships)
        eligible_managers = {pk for pk in manager_ids if _available(users.get(pk), employees)}
        receipt = {
            "status": "already_applied" if marker else "dry_run",
            "expected_sha": expected_sha, "run_id": run_id, "run_attempt": run_attempt,
            "approval_id": APPROVAL_ID,
            "eligible_manager_count": len(eligible_managers),
            "excluded_manager_count": len(manager_ids - eligible_managers),
            "granted_manager_count": 0, "pipelines_created": 0, "stages_created": 0,
        }
        if not marker and (not requester_eligible or not company_has_access(organization)):
            raise ProvisioningBlocked("crm_access_requester_or_organization_ineligible")
        before = None
        if apply and not marker:
            recipients = eligible_managers | {REQUESTER_ID}
            before = {
                "crm_v2_read": organization.crm_v2_read, "crm_v2_write": organization.crm_v2_write,
                "grants": {str(pk): {field: bool(grants.get(pk) and getattr(grants[pk], field))
                                     for field in OWN_CAPABILITIES} for pk in sorted(recipients)},
            }
            pipelines, stages = bootstrap_commercial_settings(organization.pk)
            receipt.update(pipelines_created=pipelines, stages_created=stages)
            for pk in sorted(recipients):
                grant = grants.get(pk)
                if grant is None:
                    grant = CrmAccess(organization=organization, user=users[pk])
                changed = [field for field in OWN_CAPABILITIES if not getattr(grant, field)]
                for field in changed:
                    setattr(grant, field, True)
                if grant.pk is None:
                    grant.save()
                elif changed:
                    grant.save(update_fields=changed)
                grants[pk] = grant
                if pk in eligible_managers and changed:
                    receipt["granted_manager_count"] += 1
            organization.crm_v2_read = organization.crm_v2_write = True
            organization.save(update_fields=["crm_v2_read", "crm_v2_write"])
            receipt["status"] = "applied"
        # Read back persisted capabilities and invoke the existing DB-backed policy,
        # while all current identity/policy rows remain locked in this transaction.
        organization.refresh_from_db(fields=["crm_v2_read", "crm_v2_write"])
        grants = {grant.user_id: grant for grant in CrmAccess.objects.select_for_update().filter(
            organization=organization, user_id__in=users).order_by("pk")}
        receipt.update(
            crm_v2_read=organization.crm_v2_read, crm_v2_write=organization.crm_v2_write,
            requester_access=bool(requester_eligible and _access_verified(requester, organization, grants)),
            managers_access_verified=bool(company_has_access(organization) and all(
                _access_verified(users[pk], organization, grants) for pk in eligible_managers)),
        )
        if before is not None:
            if not all(receipt[field] for field in (
                    "crm_v2_read", "crm_v2_write", "requester_access", "managers_access_verified")):
                raise ProvisioningBlocked("crm_access_readback_failed")
            DataAuditLog.objects.create(
                entity_type=AUDIT_ENTITY, entity_id=APPROVAL_ID, organization=organization,
                actor=None, action=DataAuditLog.ACTION_UPDATE, before=before,
                after={
                    "crm_v2_read": True, "crm_v2_write": True,
                    "grants": {str(pk): {field: getattr(grants[pk], field) for field in OWN_CAPABILITIES}
                               for pk in sorted(eligible_managers | {REQUESTER_ID})},
                    "operator": {
                        "source": "protected_deployment", "expected_sha": expected_sha,
                        "approval_id": APPROVAL_ID, "requester_user_id": REQUESTER_ID,
                        "description": "Enable CRM own-deal access for the approved requester and current active managers",
                        "run_id": run_id, "run_attempt": run_attempt,
                        "execution_ref": f"https://github.com/borovikov88/service2/actions/runs/{run_id}/attempts/{run_attempt}",
                    },
                    "receipt": receipt,
                },
                changed_fields=["crm_v2_read", "crm_v2_write", "grants"],
            )
        return receipt
