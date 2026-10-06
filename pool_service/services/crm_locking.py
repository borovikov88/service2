from django.db import transaction

from pool_service.models import CrmItem, ServiceTask


def lock_crm_graph(item_ids):
    """Lock linked tasks first, then CRM items, in deterministic id order."""
    connection = transaction.get_connection()
    if not connection.in_atomic_block:
        raise RuntimeError("CRM graph locks require an active transaction.")

    normalized_ids = sorted({int(item_id) for item_id in item_ids if item_id})
    if not normalized_ids:
        return [], []

    locked_tasks = list(
        ServiceTask.objects.select_for_update()
        .filter(crm_item_id__in=normalized_ids)
        .order_by("id")
    )
    locked_items = list(
        CrmItem.objects.select_for_update()
        .filter(id__in=normalized_ids)
        .order_by("id")
    )
    return locked_tasks, locked_items


def locked_crm_item(item_id):
    _tasks, items = lock_crm_graph([item_id])
    return items[0] if items else None


def locked_task_for_completion(*, organization, task_id):
    """Lock a task and, when linked to CRM, the whole task group before its CRM row."""
    seed = (
        ServiceTask.objects.filter(pk=task_id, organization=organization)
        .values("id", "crm_item_id")
        .first()
    )
    if not seed:
        return None

    crm_item_id = seed.get("crm_item_id")
    if not crm_item_id:
        return (
            ServiceTask.objects.select_for_update()
            .select_related("client", "pool", "primary_responsible")
            .prefetch_related("responsibles")
            .filter(pk=task_id, organization=organization)
            .first()
        )

    tasks, _items = lock_crm_graph([crm_item_id])
    for task in tasks:
        if task.id == task_id and task.organization_id == organization.id:
            return task
    return None
