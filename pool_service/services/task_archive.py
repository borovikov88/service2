from django.utils import timezone

from pool_service.models import CrmItem, ServiceTask


def archive_task(task, reason, user=None):
    changed = []
    if not task.is_archived:
        task.is_archived = True
        changed.append("is_archived")
    if task.archived_reason != reason:
        task.archived_reason = reason
        changed.append("archived_reason")
    now_value = timezone.now()
    if task.archived_at != now_value:
        task.archived_at = now_value
        changed.append("archived_at")
    if task.archived_by_id != getattr(user, "id", None):
        task.archived_by = user
        changed.append("archived_by")

    if reason == ServiceTask.ARCHIVE_REASON_COMPLETED:
        if not task.completed_at:
            task.completed_at = now_value
            changed.append("completed_at")
        if task.completed_by_id != getattr(user, "id", None):
            task.completed_by = user
            changed.append("completed_by")

    if changed:
        task.save(update_fields=[*changed, "updated_at"])
    if reason == ServiceTask.ARCHIVE_REASON_COMPLETED:
        from pool_service.services.task_generation import sync_crm_item_for_task

        sync_crm_item_for_task(task)
        crm_item = getattr(task, "crm_item", None)
        if task.crm_item_id and crm_item and crm_item.direction == CrmItem.DIRECTION_SERVICE and (
            not crm_item.is_archived or crm_item.archived_reason != CrmItem.ARCHIVE_REASON_COMPLETED
        ):
            crm_item.is_archived = True
            crm_item.archived_reason = CrmItem.ARCHIVE_REASON_COMPLETED
            crm_item.archived_at = now_value
            crm_item.archived_by = user
            if crm_item.stage != CrmItem.STAGE_SERVICE_DONE:
                crm_item.stage = CrmItem.STAGE_SERVICE_DONE
                crm_item.save(update_fields=["is_archived", "archived_reason", "archived_at", "archived_by", "stage", "updated_at"])
            else:
                crm_item.save(update_fields=["is_archived", "archived_reason", "archived_at", "archived_by", "updated_at"])
    return task


def restore_task(task, user=None):
    changed = []
    if task.is_archived:
        task.is_archived = False
        changed.append("is_archived")
    if task.archived_at is not None:
        task.archived_at = None
        changed.append("archived_at")
    if task.archived_reason:
        task.archived_reason = ""
        changed.append("archived_reason")
    if task.archived_by_id is not None:
        task.archived_by = None
        changed.append("archived_by")
    if changed:
        task.save(update_fields=[*changed, "updated_at"])
    from pool_service.services.task_generation import sync_crm_item_for_task

    sync_crm_item_for_task(task)
    crm_item = getattr(task, "crm_item", None)
    if (
        task.crm_item_id
        and crm_item
        and crm_item.direction == CrmItem.DIRECTION_SERVICE
        and crm_item.is_archived
        and crm_item.archived_reason == CrmItem.ARCHIVE_REASON_COMPLETED
    ):
        crm_item.is_archived = False
        crm_item.archived_at = None
        crm_item.archived_reason = ""
        crm_item.archived_by = None
        if crm_item.stage == CrmItem.STAGE_SERVICE_DONE:
            crm_item.stage = CrmItem.STAGE_SERVICE_IN_PROGRESS
            crm_item.save(update_fields=["is_archived", "archived_at", "archived_reason", "archived_by", "stage", "updated_at"])
        else:
            crm_item.save(update_fields=["is_archived", "archived_at", "archived_reason", "archived_by", "updated_at"])
    return task
