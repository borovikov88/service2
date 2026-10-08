from datetime import date, datetime
import hashlib
import json

from django.db import transaction

from pool_service.communication_models import CallAnalysis
from pool_service.models import Organization, ServiceTask
from pool_service.services.call_privacy import is_private_call


PROPOSAL_CONFIDENCE = "high"
ACTOR_EMPLOYEE = "employee"
ACTOR_CLIENT = "client"


def _parse_due_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_due_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").time()
    except (TypeError, ValueError):
        return None


def _existing_task(call, commitment_index):
    return ServiceTask.objects.filter(
        organization_id=call.organization_id,
        task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
        payload_json__source_call_id=call.id,
        payload_json__commitment_index=commitment_index,
    ).first()


def commitment_proposal_id(call_id, commitment_index, commitment):
    """Stable identity for one analysis proposal, including its exact version."""
    canonical = json.dumps(
        commitment,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return f"call-{int(call_id)}-commitment-{int(commitment_index)}-{digest}"


def _proposal(call, index, commitment):
    actor = str(commitment.get("actor") or "").strip().lower()
    confidence = str(commitment.get("confidence") or "").strip().lower()
    action = str(commitment.get("action") or "").strip()
    due_date = _parse_due_date(commitment.get("due_date"))
    due_time = _parse_due_time(commitment.get("due_time"))
    existing = _existing_task(call, index)
    can_create = commitment_is_ready_for_review(call, commitment)
    if existing:
        status = "already_created"
    elif can_create:
        status = "ready_for_review"
    else:
        status = "needs_clarification"
    return {
        "proposal_id": commitment_proposal_id(call.pk, index, commitment),
        "call_id": call.pk,
        "commitment_index": index,
        "actor": actor or None,
        "action": action[:255],
        "kind": str(commitment.get("kind") or "other")[:80],
        "confidence": confidence or None,
        "due_date": due_date.isoformat() if due_date else None,
        "due_time": due_time.strftime("%H:%M") if due_time else None,
        "evidence": str(commitment.get("evidence") or "")[:2000],
        "status": status,
        "needs_clarification": status == "needs_clarification",
        "task_id": existing.pk if existing else None,
    }


def commitment_is_ready_for_review(call, commitment):
    """Return whether a proposal has enough validated data for task creation."""
    if not isinstance(commitment, dict):
        return False
    actor = str(commitment.get("actor") or "").strip().lower()
    confidence = str(commitment.get("confidence") or "").strip().lower()
    action = str(commitment.get("action") or "").strip()
    due_date = _parse_due_date(commitment.get("due_date"))
    return bool(
        actor in {ACTOR_EMPLOYEE, ACTOR_CLIENT}
        and confidence == PROPOSAL_CONFIDENCE
        and action
        and (actor != ACTOR_CLIENT or call.client_id)
        and due_date
    )


def materialize_call_commitments(call_id):
    """Prepare reviewable proposals; ServiceTask creation belongs to Operations MCP."""

    with transaction.atomic():
        organization_id = (
            CallAnalysis.objects.filter(
                call_id=call_id,
                status=CallAnalysis.STATUS_READY,
            )
            .values_list("call__organization_id", flat=True)
            .first()
        )
        if not organization_id:
            return []
        # Personal-number mutations take the same organization lock first.
        # Whichever transaction wins determines whether proposal access is
        # allowed; there is no check-then-create privacy window.
        Organization.objects.select_for_update().get(pk=organization_id)
        analysis = (
            CallAnalysis.objects.select_for_update()
            .select_related(
                "call",
            )
            .filter(call_id=call_id, status=CallAnalysis.STATUS_READY)
            .first()
        )
        if not analysis or analysis.call.organization_id != organization_id:
            return []

        facts = analysis.facts if isinstance(analysis.facts, dict) else {}
        commitments = facts.get("commitments")
        if not isinstance(commitments, list) or not commitments:
            return []

        call = analysis.call
        if is_private_call(call):
            return []
        return [
            _proposal(call, index, commitment)
            for index, commitment in enumerate(commitments[:100])
            if isinstance(commitment, dict)
        ]
