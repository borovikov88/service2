from django.utils import timezone

from pool_service.models import CrmItem
from pool_service.services.task_archive import archive_task, restore_task
from pool_service.services.task_generation import sync_task_with_crm_item


def archive_crm_item(item, reason, user=None):
    changed = []
    if not item.is_archived:
        item.is_archived = True
        changed.append("is_archived")
    if item.archived_reason != reason:
        item.archived_reason = reason
        changed.append("archived_reason")
    now_value = timezone.now()
    item.archived_at = now_value
    changed.append("archived_at")
    if item.archived_by_id != getattr(user, "id", None):
        item.archived_by = user
        changed.append("archived_by")

    if reason == CrmItem.ARCHIVE_REASON_COMPLETED and item.direction == CrmItem.DIRECTION_SERVICE:
        if item.stage != CrmItem.STAGE_SERVICE_DONE:
            item.stage = CrmItem.STAGE_SERVICE_DONE
            changed.append("stage")

    item.save(update_fields=[*dict.fromkeys(changed), "updated_at"])

    for linked_task in item.service_tasks.all():
        archive_reason = (
            linked_task.ARCHIVE_REASON_COMPLETED
            if reason == CrmItem.ARCHIVE_REASON_COMPLETED
            else linked_task.ARCHIVE_REASON_DELETED
        )
        archive_task(linked_task, archive_reason, user or item.responsible)
    return item


def restore_crm_item(item, user=None):
    previous_reason = item.archived_reason
    changed = []
    if item.is_archived:
        item.is_archived = False
        changed.append("is_archived")
    if item.archived_at is not None:
        item.archived_at = None
        changed.append("archived_at")
    if item.archived_reason:
        item.archived_reason = ""
        changed.append("archived_reason")
    if item.archived_by_id is not None:
        item.archived_by = None
        changed.append("archived_by")
    if previous_reason == CrmItem.ARCHIVE_REASON_COMPLETED and item.direction == CrmItem.DIRECTION_SERVICE:
        if item.stage == CrmItem.STAGE_SERVICE_DONE:
            item.stage = CrmItem.STAGE_SERVICE_IN_PROGRESS
            changed.append("stage")
    if changed:
        item.save(update_fields=[*changed, "updated_at"])

    for linked_task in item.service_tasks.all():
        if linked_task.is_archived:
            restore_task(linked_task, user)
        if previous_reason == CrmItem.ARCHIVE_REASON_COMPLETED and linked_task.completed_at:
            linked_task.completed_at = None
            linked_task.completed_by = None
            linked_task.save(update_fields=["completed_at", "completed_by", "updated_at"])
        sync_task_with_crm_item(linked_task)
    return item


def sync_crm_archive_state(item, user=None):
    if item.direction != CrmItem.DIRECTION_SERVICE:
        return item
    if item.stage == CrmItem.STAGE_SERVICE_DONE:
        if not item.is_archived or item.archived_reason != CrmItem.ARCHIVE_REASON_COMPLETED:
            archive_crm_item(item, CrmItem.ARCHIVE_REASON_COMPLETED, user or item.responsible)
        return item
    if item.is_archived and item.archived_reason == CrmItem.ARCHIVE_REASON_COMPLETED:
        restore_crm_item(item, user)
    return item
