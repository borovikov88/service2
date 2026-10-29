"""SQL-only consumer for current, verified incoming communication evidence.

The inbox owns delivery/lease/retry transactions and locks connection, organization,
source and event before calling this module. No AI, provider API or client message.
"""
import hashlib
import json

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pool_service.client_queries import active_clients
from pool_service.communication_models import PhoneCall, TelephonyEmployeeIdentity
from pool_service.crm_commercial_models import (
    CrmAccess, CrmEvent, CrmIntakeActivation, CrmLead, CrmLeadSource, CrmLeadThread,
)
from pool_service.models import Client, Employee, Organization, OrganizationAccess
from pool_service.services.call_privacy import is_private_call
from pool_service.services.crm_lead_access import can_read_lead


INTAKE_OUTCOMES = frozenset({"lead_created", "lead_linked", "historical_ignored", "private_ignored"})


def _context_key(event):
    # Never match by phone/client or choose a most recent deal. A call's revisions
    # share one source identity; messages share a verified conversation namespace.
    value = (["call", event.source_key] if event.source_type == "call" else
        ["conversation", event.organization_id, event.provider_kind,
         event.connection_id_snapshot, event.account_id_snapshot, event.namespace])
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _initial_employee(source, organization, lead):
    """Use only a corroborated, current provider-to-employee mapping for D01."""
    if source.result != PhoneCall.RESULT_ANSWERED:
        return None, "missed_call"
    if not source.employee_id or not source.employee_profile_id:
        return None, "employee_unmapped"
    identities = TelephonyEmployeeIdentity.objects.select_for_update().filter(
        organization=organization, connection_id=source.connection_id, is_active=True,
        requires_manual_confirmation=False,
        status__in=(TelephonyEmployeeIdentity.STATUS_AUTO_MATCHED, TelephonyEmployeeIdentity.STATUS_MANUALLY_MATCHED),
        employee_id=source.employee_profile_id, employee__user_id=source.employee_id,
    )
    if source.provider_extension:
        identities = identities.filter(extension=source.provider_extension)
    elif source.provider_user:
        identities = identities.filter(external_user=source.provider_user)
    else:
        return None, "employee_unmapped"
    if identities.count() != 1:
        return None, "employee_ambiguous"
    employee = get_user_model().objects.select_for_update().filter(pk=source.employee_id, is_active=True).first()
    if not employee:
        return None, "employee_unavailable"
    if not OrganizationAccess.objects.select_for_update().filter(user=employee, organization=organization).exists():
        return None, "employee_unavailable"
    hr = list(Employee.objects.select_for_update().filter(user=employee, organization=organization))
    if not hr or any(not row.is_active or row.employment_status == Employee.STATUS_DISMISSED for row in hr):
        return None, "employee_unavailable"
    grant = CrmAccess.objects.select_for_update().filter(user=employee, organization=organization,
        view_leads=True, edit_own_leads=True, take_leads=True).first()
    if not grant or not can_read_lead(employee, organization, lead):
        return None, "employee_unavailable"
    return employee, "needs_qualification"


def consume_incoming(event, source, operation_identity):
    """Apply one delivery atomically. Retrying any revision never duplicates work."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("CRM intake requires the inbox transaction.")
    organization = Organization.objects.select_for_update().get(pk=event.organization_id)
    if not organization.crm_intake_process or not organization.crm_v2_read or not organization.crm_v2_write:
        raise ValueError("crm_intake_disabled")
    activation = CrmIntakeActivation.objects.select_for_update().filter(organization=organization).first()
    if activation is None:
        raise ValueError("crm_intake_activation_unavailable")
    # Capturing has existed before this feature. Activation deliberately starts a
    # new live queue, without turning its earlier backlog into today's workload.
    if (not event.scope_verified or event.historical or event.received_at < activation.starts_at
            or event.occurred_at is not None and event.occurred_at < activation.starts_at):
        return "historical_ignored"
    if event.source_type == "call" and is_private_call(source):
        return "private_ignored"
    existing = CrmLeadSource.objects.select_for_update().filter(inbox=event).first()
    if existing:
        return "lead_linked"
    thread = CrmLeadThread.objects.select_for_update().filter(
        organization=organization, context_key=_context_key(event)).first()
    created = thread is None
    if created:
        client_id = source.conversation.client_id if event.source_type == "message" else source.client_id
        if client_id and not active_clients(Client.objects.filter(pk=client_id, organization=organization)).exists():
            client_id = None
        lead = CrmLead.objects.create(
            organization=organization, client_id=client_id,
            title="Входящий звонок — требуется уточнение" if event.source_type == "call" else "Входящее обращение — требуется уточнение",
            acquisition_source=event.provider_kind if event.source_type == "message" else "",
            first_contact_channel=event.provider_kind, first_occurred_at=event.occurred_at,
            received_at=event.received_at, last_activity_at=event.occurred_at or event.received_at,
            needs_review=True, intake_reason="unassigned",
        )
        thread = CrmLeadThread.objects.create(organization=organization, context_key=_context_key(event), lead=lead)
    else:
        lead = CrmLead.objects.select_for_update().get(pk=thread.lead_id, organization=organization)
    CrmLeadSource.objects.create(organization=organization, inbox=event, lead=lead)
    before = lead.revision
    old_assignee = lead.assignee_id
    old_status = lead.status
    # First confirmed answer can fill an unassigned card. Explicit take, later
    # calls, completed qualification and manual ownership are never overwritten.
    if event.source_type == "call" and lead.assignee_id is None and not lead.result and not lead.is_archived:
        actor, reason = _initial_employee(source, organization, lead)
        lead.intake_reason = reason
        if actor:
            lead.assignee = actor
            lead.status = "working"
    stamp = event.occurred_at or event.received_at
    if lead.last_activity_at is None or stamp > lead.last_activity_at:
        lead.last_activity_at = stamp
    lead.needs_review = True
    lead.revision += 1
    lead.save()
    diff = {"revision": {"before": before, "after": lead.revision}}
    if old_assignee != lead.assignee_id:
        diff["assignee_id"] = {"before": old_assignee, "after": lead.assignee_id}
    if old_status != lead.status:
        diff["status"] = {"before": old_status, "after": lead.status}
    CrmEvent.objects.create(organization=organization, lead=lead,
        event_type="lead.incoming", occurred_at=stamp, safe_diff=diff)
    return "lead_created" if created else "lead_linked"
