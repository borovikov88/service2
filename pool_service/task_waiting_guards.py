"""Keep legacy edits and calendar dragging from reviving waiting appointments.

Normal tasks delegate to the existing views. Waiting tasks must use the explicit
feedback actions, which distinguish a new appointment from an internal check.
"""
import json

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect

from pool_service.models import ServiceTask
from pool_service.services.crm_locking import locked_task_with_crm_graph
from pool_service.services.task_feedback import has_access, waiting_control


def _locked_candidate(task_id, user):
    seed = ServiceTask.objects.filter(pk=task_id).select_related("organization").first()
    if seed is None or seed.task_type != ServiceTask.TYPE_CRM_FOLLOWUP:
        return None, None
    if not has_access(seed, user, write=True):
        return None, HttpResponseForbidden()
    task = locked_task_with_crm_graph(organization=seed.organization, task_id=task_id)
    if task is not None and not has_access(task, user, write=True):
        return None, HttpResponseForbidden()
    return task, None


def _waiting_conflict(request, task, *, as_json):
    url = reverse("task_feedback", kwargs={"task_id": task.pk})
    message = "Задача ожидает согласования даты. Используйте «Обсуждение и изменения»: перенос встречи и следующая проверка — разные действия."
    if as_json:
        return JsonResponse({
            "ok": False, "error": "waiting_task_use_feedback",
            "message": message, "feedback_url": url,
        }, status=409)
    return render(request, "pool_service/task_waiting_conflict.html", {
        "task": task, "feedback_url": url, "conflict_message": message,
        "page_title": "Задача уже переведена в ожидание",
    }, status=409)


@csrf_protect
@login_required
@transaction.atomic
def guarded_task_edit(request, task_id):
    from pool_service import views

    if request.method == "POST":
        task, denied = _locked_candidate(task_id, request.user)
        if denied is not None:
            return denied
        if task is not None and waiting_control(task)[0]:
            return _waiting_conflict(request, task, as_json=views._is_modal_request(request))
    return views.task_edit(request, task_id)


@csrf_protect
@login_required
@transaction.atomic
def guarded_task_move(request):
    from pool_service import views

    if request.method == "POST":
        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else request.POST
        except (TypeError, ValueError):
            payload = request.POST
        if not isinstance(payload, dict):
            # QueryDict also subclasses dict. Do not delegate malformed JSON.
            return JsonResponse({"ok": False, "error": "invalid_request"}, status=400)
        try:
            task_id = int(payload.get("task_id"))
        except (TypeError, ValueError):
            return views.task_move(request)
        task, denied = _locked_candidate(task_id, request.user)
        if denied is not None:
            return denied
        if task is not None and task.status == ServiceTask.STATUS_CANCELLED:
            return JsonResponse(
                {"ok": False, "error": "cancelled_task"},
                status=400,
            )
        if task is not None and waiting_control(task)[0]:
            return _waiting_conflict(request, task, as_json=True)
    return views.task_move(request)
