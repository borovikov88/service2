"""SQL-only consumer for current, verified communication evidence.

The inbox owns delivery/lease/retry transactions and locks connection, organization,
source and event before calling this module. No AI, provider API or client message.
"""
from pool_service.services.crm_role_runtime import resolve_crm_capabilities
from django.contrib.auth import get_user_model
from django.db import transaction
from pool_service.communication_models import PhoneCall, TelephonyEmployeeIdentity
from pool_service.crm_commercial_models import (
    CrmEvent, CrmIntakeActivation, CrmLead, CrmLeadSource, CrmLeadThread,
)
from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.services.crm_lead_access import can_read_lead
from pool_service.services.crm_lead_titles import automatic_call_title, call_title, has_manual_title
from pool_service.services.call_privacy import is_private_call
from .crm_lead_identity import context_hash, is_active, lead_identity, matching_active_leads, source_identity


INTAKE_OUTCOMES = frozenset({
    "lead_created", "lead_linked", "deal_linked", "needs_distribution",
    "historical_ignored", "private_ignored", "internal_ignored", "classification_pending",
})


def _context_key(event):
    # Legacy conversation pointers remain useful for unidentified participants.
    # Event deduplication MUST NOT use this movable pointer.
    value = (["call", event.source_key] if event.source_type == "call" else
        ["conversation", event.organization_id, event.provider_kind,
         event.connection_id_snapshot, event.account_id_snapshot, event.namespace])
    return context_hash(value)


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
    grant = resolve_crm_capabilities(employee.pk, organization.pk, lock=True)
    if not (grant.view_leads and grant.edit_own_leads and grant.take_leads) or not can_read_lead(employee, organization, lead):
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
    if event.source_type == "call":
        classification = source.classification_evidence if isinstance(source.classification_evidence, dict) else {}
        if classification.get("kind") == "review":
            return "classification_pending"
        if source.direction == PhoneCall.DIRECTION_INTERNAL or classification.get("kind") == "internal":
            return "internal_ignored"
    existing = CrmLeadSource.objects.select_for_update().filter(inbox=event).first()
    if existing:
        return "lead_linked"
    original = CrmLeadSource.objects.select_for_update().filter(organization=organization,
        inbox__organization=organization, inbox__source_key=event.source_key).order_by("inbox__revision", "pk").first()
    identity = source_identity(organization, source)
    thread = None
    created = False
    if original:
        lead = CrmLead.objects.select_for_update().get(pk=original.lead_id, organization=organization)
    else:
        # A verified explicit lead context establishes a single owner even if
        # the operator adds it between source capture and background delivery.
        explicit_lead = None
        work_context = None
        if event.source_type == "call":
            from pool_service.staff_call_models import CallWorkContext
            work_context = CallWorkContext.objects.filter(call=source, organization=organization).first()
            if work_context and work_context.lead_id:
                explicit_lead = CrmLead.objects.select_for_update().filter(pk=work_context.lead_id, organization=organization).first()
        from .crm_communication_routing import route_to_deal
        disposition = route_to_deal(event, organization, identity, _context_key(event), work_context)
        if disposition:
            return disposition
        matches = [explicit_lead] if explicit_lead else matching_active_leads(organization, identity)
        if len(matches) > 1:
            # Preserve retry until the conflicting leads can be resolved.
            raise ValueError("crm_active_lead_ambiguous")
        lead = matches[0] if matches else None
        thread = CrmLeadThread.objects.select_for_update().filter(
            organization=organization, context_key=_context_key(event)).first()
        if lead is None and thread:
            candidate = CrmLead.objects.select_for_update().get(pk=thread.lead_id, organization=organization)
            candidate_identity = lead_identity(candidate, validate_sources=True)
            confirmed = event.source_type == "message" and CrmLeadSource.objects.filter(
                organization=organization, lead__organization=organization,
                lead__duplicate_of=candidate, lead__status="closed", lead__result="duplicate",
                inbox__organization=organization, inbox__source_type="message",
                inbox__connection_id_snapshot=event.connection_id_snapshot,
                inbox__account_id_snapshot=event.account_id_snapshot, inbox__namespace=event.namespace).exists()
            if (is_active(candidate) and not candidate_identity.ambiguous and (confirmed or not identity.ambiguous
                    and (identity.key is None or candidate_identity.key is None or identity.key == candidate_identity.key))):
                lead = candidate
        created = lead is None
    if created:
        lead = CrmLead.objects.create(
            organization=organization, client=identity.client, contact=identity.contact, identity_origin="incoming",
            title=call_title(source.phone_number, source.result == PhoneCall.RESULT_MISSED, source.direction) if event.source_type == "call" else "Входящее обращение — требуется уточнение",
            acquisition_source=event.provider_kind if event.source_type == "message" else "",
            first_contact_channel=event.provider_kind, first_occurred_at=event.occurred_at,
            received_at=event.received_at, last_activity_at=event.occurred_at or event.received_at,
            needs_review=True, intake_reason="unassigned",
        )
    if not original:
        if thread:
            thread.lead = lead
            thread.save(update_fields=["lead"])
        else:
            CrmLeadThread.objects.create(organization=organization, context_key=_context_key(event), lead=lead)
    CrmLeadSource.objects.create(organization=organization, inbox=event, lead=lead)
    if not is_active(lead):
        return "lead_linked"  # A revision of old evidence never reopens an episode.
    before = lead.revision
    old_assignee = lead.assignee_id
    old_status = lead.status
    if not lead.next_task_id and (lead.identity_origin == "incoming" or not lead.client_id and not lead.contact_id and lead.identity_origin != "manual"):
        current_identity = lead_identity(lead)
        if not current_identity.ambiguous:
            lead.client, lead.contact = current_identity.client, current_identity.contact
    if event.source_type == "call" and not has_manual_title(lead):
        lead.title = automatic_call_title(lead.title, source.phone_number, source.result == PhoneCall.RESULT_MISSED, source.direction)
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
    assignment_event = CrmEvent.objects.create(organization=organization, lead=lead,
        event_type="lead.incoming", occurred_at=stamp, safe_diff=diff)
    if event.source_type != "call" and lead.assignee_id and old_assignee != lead.assignee_id:
        from .crm_assignment_notifications import notify_assignment
        notify_assignment(assignment_event, lead.assignee)
    return "lead_created" if created else "lead_linked"
