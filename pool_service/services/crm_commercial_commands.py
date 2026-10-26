"""The dormant, internal command boundary. No HTTP/UI or legacy writer hooks."""
from datetime import datetime, timezone as datetime_timezone
import hashlib
import json

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import router, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.crm_commercial_models import (
    CANONICAL_KEY, CrmAccess, CrmCommand, CrmDeal, CrmEvent, CrmLead,
    CrmPipeline, CrmStage,
)
from pool_service.models import Employee, Organization, OrganizationAccess


class CommercialConflict(ValidationError):
    """The key was reused or the caller's subject revision is stale."""


COMMANDS = {
    "lead.edit": (CrmLead, "assignee_id", "edit_own_leads", {"title", "need"}),
    "lead.transition": (CrmLead, "assignee_id", "transition_own_leads", {"status", "reason", "follow_up_at"}),
    "deal.edit": (CrmDeal, "manager_id", "edit_own_deals", {"title"}),
    "deal.transition": (CrmDeal, "manager_id", "transition_own_deals", {"stage_id", "reason"}),
}
# Explicit technical bounds, independent of editable labels/positions. No tender
# automation, contract, sending, execution, payments or closure in this release.
PREPARATORY_STAGES = {
    "construction": ("need", "technical", "proposal"),
    "service": ("request", "assessment", "proposal"),
    "retail": ("need", "selection", "proposal"),
    "tender": ("review", "assessment", "preparation"),
}
LEAD_STATES = ("new", "working", "qualifying", "waiting", "deferred")


def _positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValidationError("CRM identifiers must be positive integers.")
    return value


def _normalize(command_type, changes):
    if type(command_type) is not str or command_type not in COMMANDS or type(changes) is not dict:
        raise ValidationError("Unknown CRM command or payload.")
    allowed = COMMANDS[command_type][3]
    if not changes or not changes.keys() <= allowed:
        raise ValidationError("Fields are unavailable for this CRM command.")
    normalized = dict(changes)
    for key, value in normalized.items():
        if key == "stage_id":
            _positive_id(value)
        elif key == "follow_up_at":
            if value is not None:
                if isinstance(value, str):
                    try:
                        value = parse_datetime(value)
                    except ValueError as exc:
                        raise ValidationError("Invalid follow-up datetime.") from exc
                if not isinstance(value, datetime) or timezone.is_naive(value):
                    raise ValidationError("Follow-up requires a timezone-aware datetime.")
                try:
                    normalized[key] = value.astimezone(datetime_timezone.utc).isoformat()
                except (OverflowError, ValueError) as exc:
                    raise ValidationError("Follow-up datetime is outside the supported UTC range.") from exc
        elif type(value) is not str:
            raise ValidationError("CRM text must be a string.")
        else:
            normalized[key] = value.strip()
            limit = {"title": 255, "need": 10000, "reason": 500}.get(key, 16)
            if len(normalized[key]) > limit or (key == "title" and not normalized[key]):
                raise ValidationError("CRM text is empty or too long.")
    if command_type.endswith(".transition"):
        normalized.setdefault("reason", "")
    if command_type == "lead.transition":
        normalized.setdefault("follow_up_at", None)
    if command_type == "lead.transition" and "status" not in normalized:
        raise ValidationError("A lead transition requires status.")
    if command_type == "deal.transition" and "stage_id" not in normalized:
        raise ValidationError("A deal transition requires stage_id.")
    return normalized


def _authorize(using, actor_id, organization, capability):
    # Current locking reads are deliberate: never authorize from passed instances
    # or a MySQL repeatable-read snapshot. Locks are retained through replay too.
    actor = get_user_model().objects.using(using).select_for_update().filter(pk=actor_id).first()
    memberships = list(OrganizationAccess.objects.using(using).select_for_update().filter(
        user_id=actor_id, organization_id=organization.pk).order_by("pk"))
    employees = list(Employee.objects.using(using).select_for_update().filter(
        user_id=actor_id, organization_id=organization.pk).order_by("pk"))
    grant = CrmAccess.objects.using(using).select_for_update().filter(
        user_id=actor_id, organization_id=organization.pk).first()
    if (not actor or not actor.is_active or not memberships
            or not organization.crm_v2_read or not organization.crm_v2_write
            or any(not employee.is_active or employee.employment_status == Employee.STATUS_DISMISSED for employee in employees)
            or not grant or not getattr(grant, capability)
            or not (any(item.role == "manager" for item in memberships) or grant.view_team_deals)):
        raise PermissionDenied("Current CRM command access is unavailable.")


def _apply(subject, command_type, changes, using):
    before = subject.revision
    diff = {"revision": {"before": before, "after": before + 1}}
    reason = changes.get("reason", "")
    if subject.is_archived:
        raise ValidationError("Archived CRM subjects are read-only.")
    if isinstance(subject, CrmLead):
        if subject.status not in LEAD_STATES or subject.result:
            raise ValidationError("Completed lead qualification is outside this release.")
        if command_type == "lead.transition":
            status = changes["status"]
            if status not in LEAD_STATES or status == subject.status or status == "new":
                raise ValidationError("This lead transition is unavailable.")
            if LEAD_STATES.index(status) < LEAD_STATES.index(subject.status) and not reason:
                raise ValidationError("A backward transition requires a reason.")
            follow_up = changes.get("follow_up_at")
            if status in {"waiting", "deferred"} and (not reason or not follow_up):
                raise ValidationError("Waiting or deferred leads require a reason and check date.")
            diff["status"] = {"before": subject.status, "after": status}
            subject.status, subject.reason = status, reason
            subject.follow_up_at = parse_datetime(follow_up) if follow_up else None
    else:
        if subject.outcome != "open":
            raise ValidationError("Only open deals are supported.")
        # Always validate the full current graph, including on an edit.
        pipeline = CrmPipeline.objects.using(using).select_for_update().get(pk=subject.pipeline_id)
        stages = {stage.pk: stage for stage in CrmStage.objects.using(using).select_for_update().filter(
            pk__in={subject.stage_id, changes.get("stage_id", subject.stage_id)}).order_by("pk")}
        current = stages[subject.stage_id]
        if pipeline.organization_id != subject.organization_id or current.pipeline_id != pipeline.pk:
            raise ValidationError("CRM stage/pipeline scope is inconsistent.")
        if command_type == "deal.transition":
            target = stages.get(changes["stage_id"])
            order = PREPARATORY_STAGES.get(pipeline.code, ())
            if not target or target.pipeline_id != pipeline.pk:
                raise ValidationError("Target stage must belong to the current pipeline.")
            if not pipeline.is_active or not target.is_active:
                raise ValidationError("The target pipeline or stage is disabled.")
            if current.code not in order or target.code not in order:
                raise ValidationError("Stage requires a later contract, execution, payment or closure process.")
            if target.pk == current.pk:
                raise ValidationError("The deal is already at this stage.")
            if order.index(target.code) < order.index(current.code) and not reason:
                raise ValidationError("A backward transition requires a reason.")
            diff["stage_id"] = {"before": subject.stage_id, "after": target.pk}
            subject.stage = target
    if command_type.endswith(".edit"):
        changed = False
        for key, value in changes.items():
            if getattr(subject, key) != value:
                changed = True
                setattr(subject, key, value)
                diff[f"{key}_changed"] = {"before": False, "after": True}
        if not changed:
            raise ValidationError("The command has no changes.")
    subject.revision += 1
    subject.save(using=using)
    return diff, reason


def execute_commercial_command(*, actor, organization, operation_key, command_type,
                               subject_id, expected_revision, changes):
    """Apply or replay one authorized command; return structural IDs/revision.

    The caller supplies account/org identity only, never trusted cached rights.
    Failed commands leave no durable operation row and may be retried after repair.
    """
    actor_id = _positive_id(getattr(actor, "pk", None))
    organization_id = _positive_id(getattr(organization, "pk", None))
    _positive_id(subject_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValidationError("expected_revision must be a nonnegative integer.")
    if type(operation_key) is not str or len(operation_key) > 100:
        raise ValidationError("Invalid operation key.")
    CANONICAL_KEY(operation_key)  # no lowercasing/trimming aliases, even on MySQL
    changes = _normalize(command_type, changes)
    model, ownership, capability, _fields = COMMANDS[command_type]
    payload = {"actor_id": actor_id, "organization_id": organization_id,
               "command_type": command_type, "subject_id": subject_id,
               "expected_revision": expected_revision, "changes": changes}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    using = router.db_for_write(CrmCommand)
    with transaction.atomic(using=using):
        try:
            organization = Organization.objects.using(using).select_for_update().get(pk=organization_id)
        except Organization.DoesNotExist as exc:
            raise PermissionDenied("Organization unavailable.") from exc
        _authorize(using, actor_id, organization, capability)
        try:
            subject = model.objects.using(using).select_for_update().get(pk=subject_id, organization_id=organization_id)
        except model.DoesNotExist as exc:
            raise PermissionDenied("CRM subject unavailable.") from exc
        if getattr(subject, ownership) != actor_id:
            raise PermissionDenied("Only the actor's own CRM cards may be changed.")
        # Lock a current legacy client link as well. This does not turn legacy
        # writers into command writers or promise a durable tenant CHECK.
        if subject.client_id:
            from pool_service.models import Client
            client = Client.objects.using(using).select_for_update().get(pk=subject.client_id)
            if client.organization_id != organization_id:
                raise ValidationError("CRM client scope is inconsistent.")
        # Revalidate tenant links before returning even a saved result.
        subject.full_clean()
        previous = CrmCommand.objects.using(using).select_for_update().filter(
            organization_id=organization_id, operation_key=operation_key).first()
        if previous:
            if previous.actor_id != actor_id or previous.command_type != command_type or previous.payload_hash != digest:
                raise CommercialConflict("Operation key was used with different content.")
            return dict(previous.result)
        if subject.revision != expected_revision:
            raise CommercialConflict("CRM revision changed; reload the card.")
        diff, reason = _apply(subject, command_type, changes, using)
        command = CrmCommand.objects.using(using).create(
            organization=organization, operation_key=operation_key, command_type=command_type,
            payload_hash=digest, actor_id=actor_id)
        event = CrmEvent.objects.using(using).create(
            organization=organization, **{"lead" if model is CrmLead else "deal": subject},
            command=command, actor_id=actor_id, event_type=command_type, safe_diff=diff, reason=reason)
        command.result = {"lead_id" if model is CrmLead else "deal_id": subject.pk,
                          "event_id": event.pk, "revision": subject.revision}
        command.save(using=using, update_fields=["result"])
        return dict(command.result)
