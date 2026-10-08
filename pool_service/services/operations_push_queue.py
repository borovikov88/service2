"""Bounded Operations push selection, using only an indexed relational queue.

Lock order is task first, queue second; producers already hold the task lock.
The old JSON marker remains compatibility metadata, never the selection index.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from pool_service.models import ServiceTask
from pool_service.operations_models import OperationsPushQueue
from pool_service.services.call_privacy import task_source_is_private


logger = logging.getLogger(__name__)
RETRY_DELAY = timedelta(minutes=1)


def sync_queue(task, *, pending):
    """Keep queue membership atomic with the caller's locked task payload save."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Operations queue updates require an atomic task write.")
    if pending and task.task_type == ServiceTask.TYPE_CRM_FOLLOWUP:
        # Preserve an existing retry time/cursor. Replayed requests must not
        # continually move a delivery to the front of the queue.
        OperationsPushQueue.objects.get_or_create(task_id=task.pk)
    else:
        OperationsPushQueue.objects.filter(task_id=task.pk).delete()


def due_candidates(now, candidate_limit):
    return (
        OperationsPushQueue.objects.filter(next_attempt_at__lte=now)
        .order_by("next_attempt_at", "task_id")
        .values_list("task_id", flat=True)[:candidate_limit]
    )


def process_queue(*, limit=100, candidate_limit=500):
    """Claim delivery metadata under lock, then perform Web Push after commit."""
    from pool_service import operations_mcp_views as operations

    limit = max(1, min(int(limit), 500))
    candidate_limit = max(1, min(int(candidate_limit), 500))
    cutoff = timezone.now()
    task_ids = list(due_candidates(cutoff, candidate_limit))
    result = {
        "checked": 0,
        "assignment_attempts": 0,
        "notification_attempts": 0,
        "delivered": 0,
    }

    def attempts_used():
        return result["assignment_attempts"] + result["notification_attempts"]

    for task_id in task_ids:
        if attempts_used() >= limit:
            break

        claimed = None
        with transaction.atomic():
            # Claim/rotation is durable before the retry helper performs its
            # own durable attempt-commit and external side effect.
            task = ServiceTask.objects.select_for_update().filter(pk=task_id).first()
            if task is None:
                continue
            entry = (
                OperationsPushQueue.objects.select_for_update()
                .filter(task_id=task_id, next_attempt_at__lte=cutoff)
                .first()
            )
            if entry is None:
                continue

            result["checked"] += 1
            if (
                task.task_type != ServiceTask.TYPE_CRM_FOLLOWUP
                or task_source_is_private(task)
            ):
                entry.delete()
                continue

            payload = task.payload_json if isinstance(task.payload_json, dict) else {}
            pending = []
            assignment = payload.get(operations.ASSIGNMENT_DELIVERY_PAYLOAD_KEY)
            if operations._push_delivery_is_pending(assignment):
                recipient_id = (
                    assignment.get("responsible_user_id")
                    or task.primary_responsible_id
                )
                pending.append((
                    "a",
                    "assignment_attempts",
                    operations._retry_assignment_push,
                    recipient_id,
                ))
            deliveries = payload.get(
                operations.EMPLOYEE_NOTIFICATION_DELIVERIES_PAYLOAD_KEY
            )
            if isinstance(deliveries, dict):
                for marker, delivery in deliveries.items():
                    if operations._push_delivery_is_pending(delivery):
                        pending.append((
                            "n:" + str(marker),
                            "notification_attempts",
                            operations._retry_employee_notification_push,
                            marker,
                        ))
            if not pending:
                entry.delete()
                continue

            pending.sort(key=lambda item: item[0])
            ordered = [item for item in pending if item[0] > entry.last_marker]
            ordered.extend(item for item in pending if item[0] <= entry.last_marker)
            marker, counter, retry, argument = ordered[0]
            entry.next_attempt_at = timezone.now() + RETRY_DELAY
            entry.last_marker = marker
            entry.save(update_fields=["next_attempt_at", "last_marker"])
            claimed = (marker, counter, retry, argument)

        if claimed is None:
            continue

        marker, counter, retry, argument = claimed
        result[counter] += 1
        try:
            # No surrounding transaction: the retry helper's phase-1
            # attempt_committed state is a real commit before Web Push.
            delivered = retry(task_id, argument)
        except Exception as exc:
            logger.warning(
                "Operations delivery retry failed task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
            )
        else:
            result["delivered"] += int(bool(delivered))

        # If a definite non-delivery re-opened queue membership, retain
        # rotation/backoff. An ambiguous post-attempt exception leaves
        # attempt_committed terminal and therefore no automatic duplicate.
        OperationsPushQueue.objects.filter(task_id=task_id).update(
            next_attempt_at=timezone.now() + RETRY_DELAY,
            last_marker=marker,
        )

    return result

