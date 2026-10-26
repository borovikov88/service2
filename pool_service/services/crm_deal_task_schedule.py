"""Keep typed commercial next steps consistent with existing calendar editors."""
from datetime import datetime
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import F
from django.utils import timezone

from pool_service.crm_commercial_models import CrmDealTaskLink
from pool_service.models import ServiceTask


SCHEDULE_FIELDS = ("start_date", "end_date", "start_time", "end_time")


def task_schedule(task):
    return tuple(getattr(task, field) for field in SCHEDULE_FIELDS)


def synchronize_commercial_task_deadline(task, previous_schedule):
    """Called inside the existing editor transaction while its task lock is held.

    Calendar/legacy task forms edit schedule fields. Commercial commands and task
    feedback also maintain due_at; reconcile it only for these typed task links.
    Date-only schedules retain a date without inventing a midnight appointment.
    """
    if task is None or previous_schedule is None:
        return
    task.refresh_from_db()
    if task_schedule(task) == previous_schedule:
        return
    if task.crm_item_id is not None or task.task_type != ServiceTask.TYPE_CRM_FOLLOWUP:
        return
    linked = CrmDealTaskLink.objects.filter(
        task_id=task.pk, organization_id=task.organization_id,
        deal__organization_id=F("organization_id"), deal__client_id=task.client_id,
    ).exists()
    if not linked:
        return
    due = None
    clock = task.end_time or task.start_time
    if task.status != ServiceTask.STATUS_WAITING and clock:
        day = task.end_date or task.start_date
        zone = ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))
        due = timezone.make_aware(datetime.combine(day, clock), zone)
    if due != task.due_at:
        task.due_at = due
        task.save(update_fields=["due_at", "updated_at"])
