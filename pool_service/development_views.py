from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Max
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from pool_service.development_forms import (
    DevelopmentIterationForm,
    DevelopmentTaskCreateForm,
    DevelopmentTaskUpdateForm,
)
from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    DevelopmentTaskEvent,
    OrganizationAccess,
)
from pool_service.services.permissions import is_org_access_blocked, organization_for_user
from pool_service.services.development_ai import (
    PRIMARY_ANALYSIS_PURPOSE,
    check_analysis,
    launch_analysis,
    resolve_primary_analysis_iteration,
)
from pool_service.services.development_codex import (
    check_codex,
    dispatch_codex,
    github_actions_run_url,
    is_configured as codex_is_configured,
    resolve_codex_iteration,
)
from pool_service.services.development_model_selection import display_context, effective_model
from pool_service.services.development_audit import (
    HUMAN_AUDIT_NOTE_MAX_LENGTH,
    finalize_development_task_after_audit,
    human_audit_finalization_available,
)
from pool_service.services.development_review import (
    HUMAN_RESOLUTION_NOTE_MAX_LENGTH,
    HUMAN_VERDICT_APPROVE,
    HUMAN_VERDICT_CORRECTIVE,
    retry_unknown_ai_review,
    resolve_human_review,
    resolve_unknown_ai_review,
)
from pool_service.services.ai_costs import (
    codex_cost_estimate,
    cost_context,
    display_amount,
    estimate_context,
)


DEVELOPMENT_ROLES = {"owner", "admin"}


def _development_guard(request):
    organization = organization_for_user(request.user)
    if organization is None:
        return None, HttpResponseForbidden("Организация не найдена.")
    allowed = request.user.is_superuser or OrganizationAccess.objects.filter(
        user=request.user,
        organization=organization,
        role__in=DEVELOPMENT_ROLES,
    ).exists()
    if not allowed:
        return organization, HttpResponseForbidden("Недостаточно прав.")
    if is_org_access_blocked(request.user):
        messages.error(request, "Доступ организации к сервису приостановлен.")
        return organization, redirect("billing")
    return organization, None


def _task_for_organization(organization, task_id, *, lock=False):
    queryset = DevelopmentTask.objects.filter(organization=organization)
    if lock:
        queryset = queryset.select_for_update()
    return get_object_or_404(queryset, pk=task_id)


def _stage_rows(task):
    choices = list(DevelopmentTask.STAGE_CHOICES)
    current_index = next(
        (index for index, (value, _label) in enumerate(choices) if value == task.current_stage),
        0,
    )
    task_finished = task.status == DevelopmentTask.STATUS_DONE
    return [
        {
            "value": value,
            "label": label,
            "state": "done" if task_finished or index < current_index else "current" if index == current_index else "pending",
        }
        for index, (value, label) in enumerate(choices)
    ]


def _initial_analysis_prompt(task):
    return "\n".join(
        [
            "Выполни первичный технический анализ задачи разработки.",
            "",
            f"Задача: {task.reference}",
            f"Название: {task.title}",
            f"Приоритет: {task.get_priority_display()}",
            "",
            "Исходная задача:",
            task.description,
            "",
            "Бизнес-цель:",
            task.business_goal or "Не указана.",
            "",
            "Definition of Done:",
            task.definition_of_done or "Не указано.",
            "",
            "Подготовь итоговый анализ по разделам:",
            "1. Понимание задачи и ожидаемого результата.",
            "2. Текущий технический контекст и предполагаемые части приложения.",
            "3. Риски, включая security, permissions и data integrity.",
            "4. Последовательный план реализации.",
            "5. Проверка Definition of Done.",
            "6. Практические рекомендации для следующего этапа Codex.",
            "Не раскрывай chain-of-thought; нужен только итоговый технический анализ.",
            "Не выполняй deploy, миграции production или другие действия в production.",
        ]
    )


def _analysis_iteration(task):
    return resolve_primary_analysis_iteration(task)


def _analysis_context(task, iteration):
    metadata = (
        dict(iteration.automation_metadata)
        if iteration and isinstance(iteration.automation_metadata, dict)
        else {}
    )
    state = metadata.get("state", "not_started")
    labels = {
        "not_started": "AI-анализ ещё не запущен",
        "launching": "AI-анализ запускается",
        "queued": "AI-анализ поставлен в очередь",
        "in_progress": "AI выполняет первичный анализ",
        "completed": "AI-анализ завершён",
        "failed": "AI-анализ завершился ошибкой",
        "cancelled": "AI-анализ отменён",
        "incomplete": "AI-анализ завершён без результата",
        "launch_unknown": "Результат запуска требует ручной проверки",
    }
    can_launch = bool(
        iteration
        and task.status == DevelopmentTask.STATUS_ANALYSIS
        and iteration.status == DevelopmentIteration.STATUS_WORKING
        and not metadata.get("response_id")
        and not metadata.get("state")
        and settings.OPENAI_API_KEY
    )
    can_check = bool(
        iteration
        and task.status == DevelopmentTask.STATUS_ANALYSIS
        and metadata.get("response_id")
        and not metadata.get("applied")
    )
    return {
        "analysis_iteration": iteration,
        "analysis_state": state,
        "analysis_state_label": labels.get(state, "Состояние AI-анализа обновляется"),
        "analysis_can_launch": can_launch,
        "analysis_can_check": can_check,
        "openai_analysis_configured": bool(settings.OPENAI_API_KEY),
    }


def _codex_context(task):
    iteration = resolve_codex_iteration(task)
    metadata = (
        dict(iteration.automation_metadata)
        if iteration and isinstance(iteration.automation_metadata, dict)
        else {}
    )
    state = metadata.get("state", "not_started")
    labels = {
        "not_started": "Задача ещё не передана в Codex",
        "dispatching": "Запуск GitHub Actions подготавливается",
        "dispatched": "Задача передана в GitHub Actions",
        "dispatch_unknown": "Результат запуска требует ручной проверки",
        "queued": "Workflow ожидает запуска",
        "in_progress": "Codex выполняет задачу",
        "completed": "Codex создал Pull Request",
        "no_changes": "Codex не предложил изменений; результат ожидает проверки",
        "failed": "Workflow завершился ошибкой",
        "cancelled": "Workflow отменён",
        "timed_out": "Workflow превысил лимит времени",
        "validation_failed": "Codex создал PR, но проверки не прошли",
        "security_blocked": "Codex попытался изменить защищённые файлы",
        "infrastructure_failed": "Workflow завершился инфраструктурной ошибкой",
    }
    configured = codex_is_configured()
    analysis = resolve_primary_analysis_iteration(task)
    can_launch = bool(
        configured
        and task.status == DevelopmentTask.STATUS_READY_FOR_CODEX
        and analysis
        and analysis.status == DevelopmentIteration.STATUS_ACCEPTED
        and (iteration is None or metadata.get("applied"))
    )
    can_check = bool(
        configured
        and iteration
        and not metadata.get("applied")
        and task.status
        in {
            DevelopmentTask.STATUS_READY_FOR_CODEX,
            DevelopmentTask.STATUS_CODEX_WORKING,
            DevelopmentTask.STATUS_BLOCKED,
        }
    )
    workflow_run_url = github_actions_run_url(metadata.get("workflow_run_id"))
    return {
        "codex_iteration": iteration,
        "codex_metadata": metadata,
        "codex_workflow_run_url": workflow_run_url,
        "codex_state": state,
        "codex_state_label": labels.get(state, "Состояние Codex обновляется"),
        "codex_configured": configured,
        "codex_can_launch": can_launch,
        "codex_can_check": can_check,
    }


def _human_review_context(task):
    review = task.iterations.filter(
        executor_type=DevelopmentIteration.EXECUTOR_CHATGPT,
        automation_metadata__purpose="ai_review",
    ).order_by("-id").first()
    metadata = (
        dict(review.automation_metadata)
        if review and isinstance(review.automation_metadata, dict)
        else {}
    )
    available = bool(
        review
        and task.status == DevelopmentTask.STATUS_BLOCKED
        and metadata.get("purpose") == "ai_review"
        and metadata.get("decision") == "human_required"
        and metadata.get("state") == "completed"
        and metadata.get("applied") is True
        and not metadata.get("human_resolution")
    )
    recovery_available = bool(
        review
        and task.status == DevelopmentTask.STATUS_BLOCKED
        and metadata.get("purpose") == "ai_review"
        and metadata.get("decision") == "human_required"
        and metadata.get("state") == "launch_unknown"
        and metadata.get("applied") is True
        and not metadata.get("human_resolution")
    )
    return {
        "human_review_resolution_available": available,
        "human_review_iteration": review if available else None,
        "human_review_summary": review.result_summary if available else "",
        "human_review_reason": metadata.get("human_reason", "") if available else "",
        "unknown_review_recovery_available": recovery_available,
        "unknown_review_iteration": review if recovery_available else None,
        "unknown_review_reason": metadata.get("human_reason", "") if recovery_available else "",
        "human_review_note_max_length": HUMAN_RESOLUTION_NOTE_MAX_LENGTH,
    }


@login_required
def development_task_list(request):
    organization, denied = _development_guard(request)
    if denied:
        return denied

    valid_statuses = {value for value, _label in DevelopmentTask.STATUS_CHOICES}
    selected_status = (request.GET.get("status") or "").strip()
    tasks = DevelopmentTask.objects.filter(organization=organization).annotate(
        iteration_count=Count("iterations", distinct=True)
    ).prefetch_related("iterations")
    if selected_status in valid_statuses:
        tasks = tasks.filter(status=selected_status)
    else:
        selected_status = ""

    counts = dict(
        DevelopmentTask.objects.filter(organization=organization)
        .values_list("status")
        .annotate(total=Count("id"))
    )
    groups = [
        ("Новые", [DevelopmentTask.STATUS_NEW]),
        (
            "В работе",
            [
                DevelopmentTask.STATUS_ANALYSIS,
                DevelopmentTask.STATUS_READY_FOR_CODEX,
                DevelopmentTask.STATUS_CODEX_WORKING,
                DevelopmentTask.STATUS_TESTING,
                DevelopmentTask.STATUS_REVISION,
            ],
        ),
        ("На проверке", [DevelopmentTask.STATUS_REVIEW]),
        ("Заблокированные", [DevelopmentTask.STATUS_BLOCKED, DevelopmentTask.STATUS_FAILED]),
        ("Выполненные", [DevelopmentTask.STATUS_DONE]),
    ]
    status_groups = [
        {"label": label, "count": sum(counts.get(status, 0) for status in statuses)}
        for label, statuses in groups
    ]
    for task in tasks:
        costs = cost_context(
            task.iterations.all(),
            codex_expected=task.status not in {DevelopmentTask.STATUS_NEW, DevelopmentTask.STATUS_ANALYSIS},
        )
        task.ai_cost_display = display_amount(
            costs["total"] if costs["total"] is not None else costs["partial_total"],
            partial=costs["partial_total"] is not None and costs["total"] is None,
        )
    return render(
        request,
        "pool_service/development/task_list.html",
        {
            "tasks": tasks,
            "status_choices": DevelopmentTask.STATUS_CHOICES,
            "selected_status": selected_status,
            "status_groups": status_groups,
            "active_tab": "development",
        },
    )


@login_required
def development_task_create(request):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    form = DevelopmentTaskCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            task = form.save(commit=False)
            task.organization = organization
            task.initiator = request.user
            task.save()
            DevelopmentTaskEvent.objects.create(
                task=task,
                event_type=DevelopmentTaskEvent.TYPE_CREATED,
                message="Задача создана",
                actor=request.user,
            )
        messages.success(request, f"Задача {task.reference} создана.")
        return redirect("development_task_detail", task_id=task.pk)
    return render(
        request,
        "pool_service/development/task_form.html",
        {"form": form, "active_tab": "development"},
    )


@login_required
def development_task_detail(request, task_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    task = _task_for_organization(organization, task_id)
    iterations = task.iterations.select_related("executor").all()
    events = task.events.select_related("actor").all()
    analysis_iteration = _analysis_iteration(task)
    context = {
        "task": task,
        "iterations": iterations,
        "events": events,
        "stage_rows": _stage_rows(task),
        "update_form": DevelopmentTaskUpdateForm(instance=task),
        "active_tab": "development",
    }
    context.update(_analysis_context(task, analysis_iteration))
    context.update(_codex_context(task))
    context.update(_human_review_context(task))
    context["human_audit_finalization_available"] = human_audit_finalization_available(task)
    context["human_audit_note_max_length"] = HUMAN_AUDIT_NOTE_MAX_LENGTH
    context["model_selection"] = display_context(task)
    costs = cost_context(
        iterations,
        codex_expected=task.status not in {DevelopmentTask.STATUS_NEW, DevelopmentTask.STATUS_ANALYSIS},
    )
    context["ai_costs"] = costs
    context["codex_cost_estimate"] = estimate_context(
        task.automation_metadata, costs["analysis"]["amount"]
    )
    context["ai_costs_total_display"] = display_amount(
        costs["total"] if costs["total"] is not None else costs["partial_total"],
        partial=costs["partial_total"] is not None and costs["total"] is None,
    )
    for stage in ("analysis", "codex", "review"):
        costs[stage]["amount_display"] = display_amount(costs[stage]["amount"])
    return render(
        request,
        "pool_service/development/task_detail.html",
        context,
    )


@login_required
@require_POST
def development_task_start(request, task_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied

    with transaction.atomic():
        task = _task_for_organization(organization, task_id, lock=True)
        if task.status != DevelopmentTask.STATUS_NEW:
            messages.info(request, "Задача уже запущена или недоступна для запуска.")
            return redirect("development_task_detail", task_id=task.pk)

        old_status = task.status
        now = timezone.now()
        next_number = (task.iterations.aggregate(value=Max("iteration_number"))["value"] or 0) + 1

        task.status = DevelopmentTask.STATUS_ANALYSIS
        task.current_stage = DevelopmentTask.STAGE_ANALYSIS
        task.started_at = task.started_at or now
        task.current_activity = "Выполняется первичный анализ задачи"
        task.save(
            update_fields=[
                "status",
                "current_stage",
                "started_at",
                "current_activity",
                "updated_at",
            ]
        )

        iteration = DevelopmentIteration.objects.create(
            task=task,
            iteration_number=next_number,
            executor_type=DevelopmentIteration.EXECUTOR_SYSTEM,
            status=DevelopmentIteration.STATUS_WORKING,
            prompt=_initial_analysis_prompt(task),
            result_summary="Задача принята системой для первичного анализа.",
            started_at=now,
            automation_metadata={"purpose": PRIMARY_ANALYSIS_PURPOSE},
        )
        DevelopmentTaskEvent.objects.create(
            task=task,
            event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
            message="Задача запущена",
            actor=request.user,
            metadata={
                "old_status": old_status,
                "new_status": task.status,
                "iteration_id": iteration.pk,
                "iteration_number": iteration.iteration_number,
                "action": "start",
            },
        )

    result = launch_analysis(iteration.pk)
    if result.state == "not_configured":
        messages.warning(
            request,
            "Задача запущена, но интеграция OpenAI пока не настроена. AI-анализ можно запустить позже.",
        )
    elif result.state == "launch_unknown":
        messages.error(
            request,
            "Не удалось подтвердить запуск AI-анализа. Повторный запрос автоматически не выполнялся.",
        )
    elif result.state == "not_available":
        messages.error(request, "Итерация первичного AI-анализа не прошла проверку безопасности.")
    else:
        messages.success(request, "Задача запущена и передана на первичный AI-анализ.")
    return redirect("development_task_detail", task_id=task.pk)


@login_required
@require_POST
def development_task_analysis_launch(request, task_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    task = _task_for_organization(organization, task_id)
    iteration = _analysis_iteration(task)
    if iteration is None or task.status != DevelopmentTask.STATUS_ANALYSIS:
        messages.info(
            request,
            "Не удалось однозначно определить итерацию первичного AI-анализа. Запуск запрещён.",
        )
        return redirect("development_task_detail", task_id=task.pk)

    result = launch_analysis(iteration.pk)
    if result.state == "not_configured":
        messages.error(request, "Интеграция OpenAI не настроена.")
    elif result.state == "launch_unknown":
        messages.error(
            request,
            "Не удалось подтвердить запуск AI-анализа; автоматический повтор заблокирован.",
        )
    elif result.state == "not_available":
        messages.info(request, "Итерация первичного AI-анализа недоступна для запуска.")
    elif result.changed:
        messages.success(request, "Первичный AI-анализ запущен.")
    else:
        messages.info(request, "AI-анализ уже был запущен; дублирующий запрос не создан.")
    return redirect("development_task_detail", task_id=task.pk)


@login_required
@require_POST
def development_task_analysis_check(request, task_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    task = _task_for_organization(organization, task_id)
    iteration = _analysis_iteration(task)
    if iteration is None:
        messages.info(
            request,
            "Не удалось однозначно определить итерацию первичного AI-анализа. Проверка запрещена.",
        )
        return redirect("development_task_detail", task_id=task.pk)
    metadata = (
        iteration.automation_metadata
        if isinstance(iteration.automation_metadata, dict)
        else {}
    )
    if task.status != DevelopmentTask.STATUS_ANALYSIS and not metadata.get("applied"):
        messages.info(request, "Проверка AI-анализа недоступна для текущего состояния задачи.")
        return redirect("development_task_detail", task_id=task.pk)

    result = check_analysis(iteration.pk)
    if result.state == "completed":
        messages.success(request, "Первичный AI-анализ завершён.")
    elif result.state in {"failed", "cancelled", "incomplete", "launch_unknown"}:
        messages.error(request, "Первичный AI-анализ не завершён; подробности сохранены в задаче.")
    elif result.state == "check_failed":
        messages.error(request, "Не удалось проверить состояние AI-анализа. Повторите позже.")
    elif result.state in {"queued", "in_progress", "launching"}:
        messages.info(request, "AI-анализ ещё выполняется.")
    elif result.state == "task_state_changed":
        messages.warning(request, "Результат AI не применён: состояние задачи уже изменилось.")
    elif result.state == "not_available":
        messages.info(request, "Итерация первичного AI-анализа недоступна для проверки.")
    else:
        messages.info(request, "AI-анализ ещё не был запущен.")
    return redirect("development_task_detail", task_id=task.pk)


@login_required
@require_POST
def development_task_codex_start(request, task_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    task = _task_for_organization(organization, task_id)
    result = dispatch_codex(task.pk, request.user.pk)
    if result.state == "not_configured":
        messages.error(request, "Интеграция GitHub Actions не настроена.")
    elif result.state == "invalid_model":
        messages.error(request, "Модель Codex не прошла серверную проверку.")
    elif result.state == "prompt_too_large":
        messages.error(
            request,
            "Не удалось безопасно сформировать prompt Codex в пределах допустимого размера.",
        )
    elif result.state == "not_available":
        messages.info(request, "Задача недоступна для передачи в Codex.")
    elif result.state == "dispatch_unknown":
        messages.error(
            request,
            "Не удалось однозначно подтвердить запуск. Автоматический повтор заблокирован.",
        )
    elif result.state == "task_state_changed":
        messages.warning(
            request,
            "Workflow запущен, но состояние задачи уже изменилось; результат не применён.",
        )
    elif result.changed:
        messages.success(request, "Задача передана в Codex через GitHub Actions.")
    else:
        messages.info(request, "Запуск Codex уже существует; дублирующий запрос не создан.")
    return redirect("development_task_detail", task_id=task.pk)


@login_required
@require_POST
def development_task_codex_check(request, task_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    task = _task_for_organization(organization, task_id)
    result = check_codex(task.pk, request.user.pk)
    if result.state == "completed":
        messages.success(request, "Codex завершил работу; Pull Request готов к review.")
    elif result.state in {"queued", "in_progress", "dispatched"}:
        messages.info(request, "Codex ещё выполняет задачу.")
    elif result.state == "not_found":
        messages.info(request, "Запуск workflow пока не найден. Повторите проверку позже.")
    elif result.state == "check_failed":
        messages.error(request, "Не удалось проверить GitHub Actions. Повторите позже.")
    elif result.state in {
        "failed",
        "cancelled",
        "timed_out",
        "validation_failed",
        "security_blocked",
        "infrastructure_failed",
    }:
        messages.error(request, "Выполнение Codex требует ручной проверки.")
    elif result.state == "not_configured":
        messages.error(request, "Интеграция GitHub Actions не настроена.")
    elif result.state == "task_state_changed":
        messages.warning(request, "Результат не применён: состояние задачи уже изменилось.")
    else:
        messages.info(request, "Запуск Codex недоступен для проверки.")
    return redirect("development_task_detail", task_id=task.pk)


@login_required
@require_POST
def development_task_review_resolve(request, task_id, review_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    task = _task_for_organization(organization, task_id)
    get_object_or_404(task.iterations, pk=review_id)
    result = resolve_human_review(
        task.pk,
        review_id,
        request.user.pk,
        request.POST.get("verdict"),
        request.POST.get("note", ""),
    )
    if result.state == HUMAN_VERDICT_APPROVE:
        messages.success(request, "Решение принято: задача готова к деплою.")
    elif result.state == HUMAN_VERDICT_CORRECTIVE:
        messages.success(request, "Запрошена корректировка; задача возвращена в разработку.")
    elif result.state == "conflict":
        messages.error(request, "AI Review уже разрешена другим решением.")
    elif result.state == "note_required":
        messages.error(request, "Для корректировки необходимо указать инструкции.")
    elif result.state == "invalid_note":
        messages.error(request, "Комментарий слишком длинный.")
    else:
        messages.info(request, "AI Review недоступна для разрешения.")
    return redirect("development_task_detail", task_id=task.pk)


@login_required
@require_POST
def development_task_review_retry_unknown(request, task_id, review_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    task = _task_for_organization(organization, task_id)
    get_object_or_404(task.iterations, pk=review_id)
    result = retry_unknown_ai_review(task.pk, review_id, request.user.pk)
    if result.state == "retry_authorized":
        if result.changed:
            messages.success(request, "Повторный AI Review разрешён и ожидает запуска.")
        else:
            messages.info(request, "Повторный AI Review уже был разрешён.")
    else:
        messages.error(request, "Повторный AI Review недоступен для текущего состояния.")
    return redirect("development_task_detail", task_id=task.pk)


@login_required
@require_POST
def development_task_review_resolve_unknown(request, task_id, review_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    task = _task_for_organization(organization, task_id)
    get_object_or_404(task.iterations, pk=review_id)
    result = resolve_unknown_ai_review(
        task.pk,
        review_id,
        request.user.pk,
        request.POST.get("verdict"),
        request.POST.get("note", ""),
    )
    if result.state == HUMAN_VERDICT_APPROVE:
        messages.success(request, "Ручная проверка принята: задача готова к деплою.")
    elif result.state == HUMAN_VERDICT_CORRECTIVE:
        messages.success(request, "Запрошена корректировка; задача возвращена в разработку.")
    elif result.state == "note_required":
        messages.error(request, "Укажите обязательный комментарий технического руководителя.")
    elif result.state == "invalid_note":
        messages.error(request, "Комментарий слишком длинный.")
    elif result.state == "conflict":
        messages.error(request, "Неопределённый AI Review уже разрешён другим решением.")
    else:
        messages.error(request, "Ручное разрешение недоступно для текущего состояния.")
    return redirect("development_task_detail", task_id=task.pk)


@login_required
@require_POST
def development_task_finalize_audit(request, task_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    task = _task_for_organization(organization, task_id)
    result = finalize_development_task_after_audit(
        task.pk,
        request.user.pk,
        request.POST.get("note", ""),
    )
    if result.state == "finalized":
        messages.success(request, "Задача завершена после подтверждённого аудита.")
    elif result.state == "already_finalized":
        messages.info(request, "Задача уже завершена этим аудитом.")
    elif result.state == "already_done":
        messages.info(request, "Задача уже была завершена ранее.")
    elif result.state == "note_required":
        messages.error(request, "Укажите комментарий по аудиту.")
    elif result.state == "invalid_note":
        messages.error(request, "Комментарий по аудиту слишком длинный.")
    else:
        messages.error(request, "Текущее состояние задачи не допускает завершение после аудита.")
    return redirect("development_task_detail", task_id=task.pk)


@login_required
@require_POST
def development_task_update(request, task_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    with transaction.atomic():
        task = _task_for_organization(organization, task_id, lock=True)
        old_status = task.status
        form = DevelopmentTaskUpdateForm(request.POST, instance=task)
        if not form.is_valid():
            iterations = task.iterations.select_related("executor").all()
            events = task.events.select_related("actor").all()
            return render(
                request,
                "pool_service/development/task_detail.html",
                {
                    "task": task,
                    "iterations": iterations,
                    "events": events,
                    "stage_rows": _stage_rows(task),
                    "update_form": form,
                    "active_tab": "development",
                },
                status=400,
            )
        task = form.save(commit=False)
        metadata = dict(task.automation_metadata) if isinstance(task.automation_metadata, dict) else {}
        mode = form.cleaned_data["model_selection_mode"]
        if metadata.get("auto_selected_model"):
            metadata["effective_model"] = effective_model(mode, metadata["auto_selected_model"])
            metadata["codex_cost_estimate"] = codex_cost_estimate(
                metadata.get("auto_complexity"), metadata["effective_model"]
            )
        if "model_selection_mode" in request.POST or metadata.get("auto_selected_model"):
            metadata["model_selection_mode"] = mode
        task.automation_metadata = metadata
        now = timezone.now()
        if task.status != DevelopmentTask.STATUS_NEW and task.started_at is None:
            task.started_at = now
        terminal_statuses = {
            DevelopmentTask.STATUS_DONE,
            DevelopmentTask.STATUS_FAILED,
            DevelopmentTask.STATUS_CANCELLED,
        }
        task.completed_at = task.completed_at or now if task.status in terminal_statuses else None
        task.save()
        if old_status != task.status:
            DevelopmentTaskEvent.objects.create(
                task=task,
                event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
                message=f"Статус изменён: {dict(DevelopmentTask.STATUS_CHOICES)[old_status]} → {task.get_status_display()}",
                actor=request.user,
                metadata={"old_status": old_status, "new_status": task.status},
            )
        elif form.changed_data:
            DevelopmentTaskEvent.objects.create(
                task=task,
                event_type=DevelopmentTaskEvent.TYPE_NOTE,
                message="Карточка задачи обновлена",
                actor=request.user,
                metadata={"changed_fields": form.changed_data},
            )
    messages.success(request, "Состояние задачи обновлено.")
    return redirect("development_task_detail", task_id=task.pk)


@login_required
def development_iteration_create(request, task_id):
    organization, denied = _development_guard(request)
    if denied:
        return denied
    task = _task_for_organization(organization, task_id)
    form = DevelopmentIterationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            task = _task_for_organization(organization, task_id, lock=True)
            next_number = (task.iterations.aggregate(value=Max("iteration_number"))["value"] or 0) + 1
            iteration = form.save(commit=False)
            iteration.task = task
            iteration.iteration_number = next_number
            iteration.executor = request.user if iteration.executor_type == DevelopmentIteration.EXECUTOR_HUMAN else None
            iteration.save()
            task.updated_at = timezone.now()
            task.save(update_fields=["updated_at"])
            DevelopmentTaskEvent.objects.create(
                task=task,
                event_type=DevelopmentTaskEvent.TYPE_ITERATION_ADDED,
                message=f"Добавлена итерация #{next_number}: {iteration.get_executor_type_display()}",
                actor=request.user,
                metadata={"iteration_id": iteration.pk, "iteration_number": next_number},
            )
            if iteration.test_result or iteration.tests_passed or iteration.tests_failed:
                DevelopmentTaskEvent.objects.create(
                    task=task,
                    event_type=DevelopmentTaskEvent.TYPE_TEST_RESULT,
                    message=f"Тесты итерации #{next_number}: {iteration.tests_passed} passed / {iteration.tests_failed} failed",
                    actor=request.user,
                    metadata={
                        "iteration_id": iteration.pk,
                        "tests_passed": iteration.tests_passed,
                        "tests_failed": iteration.tests_failed,
                    },
                )
        messages.success(request, f"Итерация #{next_number} добавлена.")
        return redirect("development_task_detail", task_id=task.pk)
    return render(
        request,
        "pool_service/development/iteration_form.html",
        {"task": task, "form": form, "active_tab": "development"},
    )
