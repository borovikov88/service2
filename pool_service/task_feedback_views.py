from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from django import forms
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from pool_service.models import ServiceTask
from pool_service.services.task_feedback import (
    ACTION_LABELS, FeedbackConflict, apply_feedback, has_access,
    history_for, state_for, version_for, waiting_control,
)
from pool_service.services.call_privacy import task_source_is_private


class TaskFeedbackForm(forms.Form):
    action = forms.ChoiceField(label="Что сделать", choices=list(ACTION_LABELS.items()))
    comment = forms.CharField(label="Что изменилось / результат / причина", max_length=2000,
                              widget=forms.Textarea(attrs={"rows": 4}))
    due_date = forms.DateField(label="Новая дата выполнения", required=False,
                               widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    due_time = forms.TimeField(label="Новое время (необязательно)", required=False,
                               widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"))
    check_date = forms.DateField(label="Дата следующей проверки", required=False,
                                 widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    check_time = forms.TimeField(label="Время следующей проверки", required=False,
                                 widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"))
    expected_version = forms.CharField(widget=forms.HiddenInput, max_length=64)
    request_id = forms.UUIDField(widget=forms.HiddenInput)

    def clean(self):
        data = super().clean()
        action = data.get("action")
        if action == "reschedule" and not data.get("due_date"):
            self.add_error("due_date", "Укажите новую дату.")
        if action == "wait":
            if not data.get("check_date"):
                self.add_error("check_date", "Когда вернуться к согласованию?")
            if not data.get("check_time"):
                self.add_error("check_time", "Укажите время внутренней проверки.")
            if data.get("check_date") and data.get("check_time"):
                zone = ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))
                next_check = timezone.make_aware(datetime.combine(data["check_date"], data["check_time"]), zone)
                if next_check <= timezone.now():
                    self.add_error("check_date", "Следующая проверка должна быть в будущем.")
                data["next_check_at"] = next_check
        return data


@login_required
@require_http_methods(["GET", "POST"])
def task_feedback(request, task_id):
    from pool_service.views import _deny_superuser_write, _redirect_if_access_blocked
    blocked = _redirect_if_access_blocked(request)
    if blocked:
        return blocked
    task = get_object_or_404(ServiceTask.objects.select_related("organization"), pk=task_id)
    if task_source_is_private(task):
        raise PermissionDenied
    if task.task_type != ServiceTask.TYPE_CRM_FOLLOWUP or not has_access(task, request.user):
        raise PermissionDenied
    can_write = has_access(task, request.user, write=True)
    is_closed = bool(task.is_archived or task.completed_at or task.status in {ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED})
    form = TaskFeedbackForm(request.POST if request.method == "POST" else None, initial={
        "action": "comment", "request_id": uuid4(), "expected_version": version_for(task),
    })
    status = 200
    if request.method == "POST":
        readonly = _deny_superuser_write(request)
        if readonly:
            return readonly
        if not can_write:
            raise PermissionDenied
        if form.is_valid():
            data = form.cleaned_data
            try:
                _task, _event, created = apply_feedback(
                    task_id=task.pk, user=request.user, action=data["action"],
                    comment=data["comment"], expected_version=data["expected_version"],
                    request_id=data["request_id"],
                    due_date=data.get("due_date") if data["action"] == "reschedule" else None,
                    due_time=data.get("due_time") if data["action"] == "reschedule" else None,
                    next_check_at=data.get("next_check_at") if data["action"] == "wait" else None,
                )
            except FeedbackConflict as exc:
                form.add_error(None, exc)
                status = 409
            except ValidationError as exc:
                form.add_error(None, exc)
                status = 400
            else:
                messages.success(request, "Изменение сохранено." if created else "Это изменение уже сохранено; повтор не создан.")
                return redirect("task_feedback", task_id=task.pk)
        else:
            status = 400
    waiting, next_check = waiting_control(task)
    return render(request, "pool_service/task_feedback.html", {
        "task": task, "form": form, "can_write": can_write and not is_closed,
        "history": history_for(task), "feedback_state": state_for(task),
        "waiting_without_date": waiting, "next_check_at": next_check,
        "communication_zone": getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"),
    }, status=status)
