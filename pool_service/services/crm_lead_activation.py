"""One scoped activation of the approved incoming-lead release.

Recipients are limited to the previously approved CRM cohort, checked again
against current roles and HR. Replays never restore a revoked capability.
"""
from django.contrib.auth import get_user_model
from django.db import transaction

from pool_service.crm_commercial_models import CrmAccess, CrmIntakeActivation
from pool_service.models import DataAuditLog, Employee, Organization, OrganizationAccess
from pool_service.services.crm_approved_access import (
    APPROVAL_ID as PREVIOUS_APPROVAL_ID, AUDIT_ENTITY as PREVIOUS_AUDIT_ENTITY,
    ORGANIZATION_ID, REQUESTER_ID, ProvisioningBlocked, _available,
)
from pool_service.services.crm_lead_access import can_view_leads
from pool_service.services.crm_pilot_activation import OWN_CAPABILITIES
from pool_service.services.permissions import company_has_access

APPROVAL_ID = "crm-lead-intake-aqualine-20261026"
AUDIT_ENTITY = "crm_lead_intake_access"
LEAD_CAPABILITIES = ("view_leads", "create_own_leads", "take_leads", "edit_own_leads",
                     "transition_own_leads", "qualify_own_leads")


def provision(*, expected_sha, run_id=0, run_attempt=0, apply=False):
    with transaction.atomic():
        org = Organization.objects.select_for_update().filter(pk=ORGANIZATION_ID).first()
        if not org:
            raise ProvisioningBlocked("crm_access_organization_unavailable")
        previous = DataAuditLog.objects.select_for_update().filter(organization=org,
            entity_type=PREVIOUS_AUDIT_ENTITY, entity_id=PREVIOUS_APPROVAL_ID).first()
        if not previous or not isinstance(previous.after, dict) or not isinstance(previous.after.get("grants"), dict):
            raise ProvisioningBlocked("crm_access_previous_cohort_unavailable")
        raw_ids = previous.after["grants"]
        if not raw_ids or any(not isinstance(pk, str) or not pk.isascii() or not pk.isdigit() or len(pk) > 18 for pk in raw_ids):
            raise ProvisioningBlocked("crm_access_previous_cohort_unavailable")
        cohort = {int(pk) for pk in raw_ids}
        if REQUESTER_ID not in cohort:
            raise ProvisioningBlocked("crm_access_previous_cohort_unavailable")
        marker = DataAuditLog.objects.select_for_update().filter(organization=org,
            entity_type=AUDIT_ENTITY, entity_id=APPROVAL_ID).first()
        users = {user.pk: user for user in get_user_model().objects.select_for_update().filter(pk__in=cohort).order_by("pk")}
        memberships = list(OrganizationAccess.objects.select_for_update().filter(organization=org, user_id__in=cohort).order_by("pk"))
        employees = list(Employee.objects.select_for_update().filter(organization=org, user_id__in=cohort).order_by("pk"))
        grants = {grant.user_id: grant for grant in CrmAccess.objects.select_for_update().filter(organization=org, user_id__in=cohort).order_by("pk")}
        manager_ids = {row.user_id for row in memberships if row.role == "manager"}
        def eligible(pk):
            grant = grants.get(pk)
            return _available(users.get(pk), employees) and grant and all(getattr(grant, name) for name in OWN_CAPABILITIES)
        managers = {pk for pk in manager_ids if eligible(pk)}
        requester_ok = bool(eligible(REQUESTER_ID) and any(row.user_id == REQUESTER_ID and row.role == "admin" for row in memberships))
        receipt = {"status": "already_applied" if marker else "dry_run", "expected_sha": expected_sha,
            "run_id": run_id, "run_attempt": run_attempt, "approval_id": APPROVAL_ID,
            "eligible_manager_count": len(managers), "excluded_manager_count": len((cohort - {REQUESTER_ID}) - managers),
            "granted_manager_count": 0, "pipelines_created": 0, "stages_created": 0}
        activation = CrmIntakeActivation.objects.select_for_update().filter(organization=org).first()
        if not marker and (not requester_ok or not company_has_access(org) or not org.crm_v2_read or not org.crm_v2_write):
            raise ProvisioningBlocked("crm_access_requester_or_organization_ineligible")
        if not marker and (activation is not None or org.crm_intake_process):
            raise ProvisioningBlocked("crm_access_intake_state_requires_review")
        before = None
        if apply and not marker:
            recipients = managers | {REQUESTER_ID}
            before = {"crm_intake_process": False, "grants": {
                str(pk): {name: getattr(grants[pk], name) for name in LEAD_CAPABILITIES} for pk in sorted(recipients)}}
            for pk in sorted(recipients):
                changed = [name for name in LEAD_CAPABILITIES if not getattr(grants[pk], name)]
                for name in changed:
                    setattr(grants[pk], name, True)
                if changed:
                    grants[pk].save(update_fields=changed)
                if pk in managers and changed:
                    receipt["granted_manager_count"] += 1
            activation = CrmIntakeActivation.objects.create(organization=org)
            org.crm_intake_process = True
            org.save(update_fields=["crm_intake_process"])
            receipt["status"] = "applied"
        def verified(pk):
            return bool(company_has_access(org) and pk in grants and
                all(getattr(grants[pk], name) for name in LEAD_CAPABILITIES) and can_view_leads(users[pk], org))
        receipt.update(crm_v2_read=org.crm_v2_read, crm_v2_write=org.crm_v2_write,
            crm_intake_process=org.crm_intake_process,
            starts_at=activation.starts_at.isoformat() if activation else None,
            requester_access=bool(requester_ok and verified(REQUESTER_ID)),
            managers_access_verified=bool(company_has_access(org) and all(verified(pk) for pk in managers)))
        if before is not None:
            if not all(receipt[key] for key in ("crm_v2_read", "crm_v2_write", "crm_intake_process", "requester_access", "managers_access_verified")):
                raise ProvisioningBlocked("crm_access_readback_failed")
            DataAuditLog.objects.create(organization=org, entity_type=AUDIT_ENTITY, entity_id=APPROVAL_ID,
                actor=None, action=DataAuditLog.ACTION_UPDATE, before=before,
                after={"crm_intake_process": True, "starts_at": receipt["starts_at"],
                    "grants": {str(pk): {name: getattr(grants[pk], name) for name in LEAD_CAPABILITIES} for pk in sorted(managers | {REQUESTER_ID})},
                    "operator": {"source": "protected_deployment", "expected_sha": expected_sha,
                        "approval_id": APPROVAL_ID, "requester_user_id": REQUESTER_ID,
                        "run_id": run_id, "run_attempt": run_attempt}, "receipt": receipt},
                changed_fields=["crm_intake_process", "starts_at", "grants"])
        return receipt
