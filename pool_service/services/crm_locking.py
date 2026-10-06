from django.db import transaction
from django.db.models import Q

from pool_service.models import CrmItem, ServiceTask


def lock_crm_graph(item_ids, *, extra_task_ids=()):
    """Lock task rows first, then CRM items, in deterministic id order."""
    connection = transaction.get_connection()
    if not connection.in_atomic_block:
        raise RuntimeError("CRM graph locks require an active transaction.")

    normalized_item_ids = sorted({int(item_id) for item_id in item_ids if item_id})
    normalized_task_ids = sorted({int(task_id) for task_id in extra_task_ids if task_id})
    if not normalized_item_ids and not normalized_task_ids:
        return [], []

    task_filter = Q()
    if normalized_item_ids:
        task_filter |= Q(crm_item_id__in=normalized_item_ids)
    if normalized_task_ids:
        task_filter |= Q(id__in=normalized_task_ids)

    locked_tasks = list(
        ServiceTask.objects.select_for_update()
        .filter(task_filter)
        .order_by("id")
    )
    locked_items = list(
        CrmItem.objects.select_for_update()
        .filter(id__in=normalized_item_ids)
        .order_by("id")
    )
    return locked_tasks, locked_items


def locked_crm_item(item_id):
    _tasks, items = lock_crm_graph([item_id])
    return items[0] if items else None


def locked_task_with_crm_graph(*, organization, task_id):
    """Lock a task group and its CRM item using the shared task->CRM order."""
    seed = (
        ServiceTask.objects.filter(pk=task_id, organization=organization)
        .values("id", "crm_item_id")
        .first()
    )
    if not seed:
        return None

    crm_item_id = seed.get("crm_item_id")
    if not crm_item_id:
        tasks, _items = lock_crm_graph([], extra_task_ids=[task_id])
    else:
        tasks, _items = lock_crm_graph([crm_item_id])

    for task in tasks:
        if task.id == task_id and task.organization_id == organization.id:
            return task
    return None


def locked_task_for_completion(*, organization, task_id):
    return locked_task_with_crm_graph(organization=organization, task_id=task_id)
