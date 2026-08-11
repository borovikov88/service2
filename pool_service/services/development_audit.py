from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from pool_service.models import DevelopmentTask, DevelopmentTaskEvent
from pool_service.services.development_review import unresolved_launch_unknown_review


HUMAN_AUDIT_NOTE_MAX_LENGTH = 2000
HUMAN_AUDIT_ACTIVITY = "Human audit confirmed; task completed"
HUMAN_AUDIT_ACTION = "human_audit_finalized"
HUMAN_AUDIT_ALLOWED_STATUSES = {
    DevelopmentTask.STATUS_REVIEW,
    DevelopmentTask.STATUS_BLOCKED,
    DevelopmentTask.STATUS_READY_FOR_DEPLOY,
    DevelopmentTask.STATUS_REVISION,
}


@dataclass(frozen=True)
class HumanAuditFinalizationResult:
    state: str
    changed: bool = False
    task_id: int | None = None


def human_audit_finalization_available(task):
    return (
        task.status in HUMAN_AUDIT_ALLOWED_STATUSES
        and unresolved_launch_unknown_review(task) is None
    )


def finalize_development_task_after_audit(task_id, actor_id, note):
    """Finalize an audited task without changing automation history or external state."""
    note = str(note or "").strip()
    if len(note) > HUMAN_AUDIT_NOTE_MAX_LENGTH:
        return HumanAuditFinalizationResult("invalid_note", task_id=task_id)

    operation_key = f"task:{task_id}:human-audit-finalization"
    with transaction.atomic():
        task = DevelopmentTask.objects.select_for_update().get(pk=task_id)
        existing = task.events.filter(
            metadata__action=HUMAN_AUDIT_ACTION,
            metadata__operation_key=operation_key,
        ).exists()
        if task.status == DevelopmentTask.STATUS_DONE:
            state = "already_finalized" if existing else "already_done"
            return HumanAuditFinalizationResult(state, False, task.pk)
        if not note:
            return HumanAuditFinalizationResult("note_required", task_id=task.pk)
        if not human_audit_finalization_available(task):
            return HumanAuditFinalizationResult("not_available", task_id=task.pk)

        now = timezone.now()
        old_status = task.status
        task.status = DevelopmentTask.STATUS_DONE
        task.current_stage = DevelopmentTask.STAGE_COMPLETION
        task.current_activity = HUMAN_AUDIT_ACTIVITY
        task.blockers = ""
        task.completed_at = now
        task.save(
            update_fields=[
                "status",
                "current_stage",
                "current_activity",
                "blockers",
                "completed_at",
                "updated_at",
            ]
        )
        DevelopmentTaskEvent.objects.create(
            task=task,
            event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
            message=HUMAN_AUDIT_ACTIVITY,
            actor_id=actor_id,
            metadata={
                "action": HUMAN_AUDIT_ACTION,
                "actor_id": actor_id,
                "old_status": old_status,
                "new_status": DevelopmentTask.STATUS_DONE,
                "note": note,
                "operation_key": operation_key,
                "finalized_at": now.isoformat(),
            },
        )
    return HumanAuditFinalizationResult("finalized", True, task.pk)
