"""Explicit appointment/check labels for both normal and modal task cards."""
from zoneinfo import ZoneInfo

from django import template
from django.conf import settings

from pool_service.services.task_feedback import state_for, waiting_control

register = template.Library()


@register.simple_tag
def task_waiting_details(task):
    waiting, check = waiting_control(task)
    if not waiting:
        return {"is_waiting": False}
    zone = ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))
    state = state_for(task)
    return {
        "is_waiting": True,
        "next_check_label": check.astimezone(zone).strftime("%d.%m.%Y %H:%M") if check else "Не указана",
        "reason": state.get("reason", ""),
    }
