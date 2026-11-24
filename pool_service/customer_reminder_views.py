from pathlib import Path
from shlex import join as shell_join

from django.conf import settings
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from pool_service.communication_models import PhoneCall
from pool_service.communication_services import selected_communication_access
from pool_service.crm_followup_models import CustomerReminderPolicy, MissedCallFollowup
from pool_service.models import Organization
from pool_service.services.call_access import can_read_call
from pool_service.services.call_processing_settings import _lock_owner, settings_allowed
from pool_service.services.customer_reminder_calendar import BARNAUL, working_start
from pool_service.services.customer_reminders import (
    RecipientDirectory, Work, callback_candidates, eligible_users, may_see, queue_url,
    lead_for_call, returned_call, work_items,
)


class ReminderSettingsForm(forms.Form):
    enabled = forms.BooleanField(required=False, label="Напоминания включены")


class CallbackForm(forms.Form):
    next_attempt_at = forms.DateTimeField(required=False)
    note = forms.CharField(required=False, max_length=250)


def _access(request):
    access = selected_communication_access(request)
    if not access or not eligible_users(access.organization).filter(pk=request.user.pk).exists():
        raise PermissionDenied
    return access


def _queue_rows(organization, user, policy):
    directory = RecipientDirectory(policy, timezone.now()) if policy else None
    for work in work_items(organization):
        if directory:
            can_handle = directory.can_handle(work, user)
            if can_handle or user in directory.escalation(work):
                yield work, can_handle
        elif may_see(work, user, organization):
            yield work, False


@login_required
@never_cache
@require_http_methods(["GET"])
def followups(request):
    access = _access(request)
    organization = access.organization
    policy = CustomerReminderPolicy.objects.filter(organization=organization).first()
    groups = {}
    for work, can_handle in sorted(_queue_rows(organization, request.user, policy), key=lambda row: row[0].started_at):
        group = groups.setdefault(work.group_key, [])
        group.append({
            "label": work.label,
            "title": work.title,
            "url": work.url,
            "since": work.started_at.astimezone(BARNAUL),
            "retry": work.retry,
            "call_id": work.source.pk if work.kind == "call" and (
                can_handle if policy else access.role == "owner" or work.responsible_id == request.user.pk
            ) else None,
        })
    return render(request, "pool_service/communications/customer_followups.html", {
        "active_tab": "communications",
        "communication_org_id": organization.pk,
        "groups": groups.values(),
        "can_configure": settings_allowed(request.user, organization),
        "policy": policy,
    })


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def reminder_settings(request):
    access = _access(request)
    organization = access.organization
    if not settings_allowed(request.user, organization):
        raise PermissionDenied
    policy = CustomerReminderPolicy.objects.filter(organization=organization).first()
    form = ReminderSettingsForm(
        request.POST if request.method == "POST" else None,
        initial={"enabled": bool(policy and policy.started_at)},
    )
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            owner = _lock_owner(request.user, organization)
            policy = CustomerReminderPolicy.objects.select_for_update().filter(organization=organization).first()
            start = None
            if form.cleaned_data["enabled"]:
                start = (policy.started_at if policy else None) or timezone.now()
            CustomerReminderPolicy.objects.update_or_create(
                organization=organization, defaults={"fallback_owner": owner, "started_at": start},
            )
        messages.success(request, "Настройки напоминаний сохранены.")
        return redirect(queue_url(organization))
    runner = Path(settings.BASE_DIR) / "scripts" / "run_customer_reminders.sh"
    cron_command = shell_join(["/bin/bash", str(runner), str(organization.pk)]) + " >/dev/null"
    return render(request, "pool_service/communications/customer_followup_settings.html", {
        "active_tab": "communications", "form": form, "queue_url": queue_url(organization),
        "cron_command": cron_command, "last_tick_at": policy.last_tick_at if policy else None,
    })


@login_required
@require_POST
def record_callback(request, call_id):
    access = _access(request)
    organization = access.organization
    target = queue_url(organization)
    now = timezone.now()
    # Browser datetime-local inputs use Barnaul regardless of the CRM storage timezone.
    with timezone.override(BARNAUL):
        form = CallbackForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Укажите время следующего звонка или кратко запишите результат.")
            return redirect(target)
    planned = form.cleaned_data["next_attempt_at"]
    outcome = form.cleaned_data["note"]
    action = request.POST.get("action")
    if action == "schedule":
        if not planned or planned <= now or planned != working_start(planned):
            messages.error(request, "Выберите будущее время с 09:00 до 18:00 в рабочий день.")
            return redirect(target)
    elif action != "resolve" or not outcome:
        messages.error(request, "Укажите, чем закончилась связь с клиентом.")
        return redirect(target)

    with transaction.atomic():
        Organization.objects.select_for_update().get(pk=organization.pk)
        get_object_or_404(PhoneCall.objects.select_for_update(), pk=call_id, organization=organization)
        call = get_object_or_404(callback_candidates(organization), pk=call_id)
        access = _access(request)
        if access.organization_id != organization.pk:
            raise PermissionDenied
        if not can_read_call(request.user, call):
            raise Http404
        policy = CustomerReminderPolicy.objects.filter(organization=organization).first()
        work = Work("call", call, call.started_at, call.pk, lead_for_call(organization, call))
        if policy:
            if not RecipientDirectory(policy, now).can_handle(work, request.user):
                raise PermissionDenied
        elif access.role != "owner" and request.user.pk != work.responsible_id:
            raise PermissionDenied
        if returned_call(call):
            messages.info(request, "По этому обращению уже есть состоявшийся звонок.")
            return redirect(target)
        MissedCallFollowup.objects.update_or_create(call=call, defaults={
            "actor": request.user, "next_attempt_at": planned if action == "schedule" else None,
            "resolved_at": now if action == "resolve" else None, "note": outcome,
        })
    messages.success(request, "Следующий звонок запланирован." if action == "schedule" else "Результат звонка записан.")
    return redirect(target)

