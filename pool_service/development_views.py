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
from pool_service.services.development_ai import check_analysis, launch_analysis


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
    return (
        task.iterations.filter(executor_type=DevelopmentIteration.EXECUTOR_SYSTEM)
        .order_by("-iteration_number", "-id")
        .first()
    )


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


@login_required
def development_task_list(request):
    organization, denied = _development_guard(request)
    if denied:
        return denied

    valid_statuses = {value for value, _label in DevelopmentTask.STATUS_CHOICES}
    selected_status = (request.GET.get("status") or "").strip()
    tasks = DevelopmentTask.objects.filter(organization=organization).annotate(
        iteration_count=Count("iterations", distinct=True)
    )
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
        messages.info(request, "Первичный AI-анализ недоступен для текущего состояния задачи.")
        return redirect("development_task_detail", task_id=task.pk)

    result = launch_analysis(iteration.pk)
    if result.state == "not_configured":
        messages.error(request, "Интеграция OpenAI не настроена.")
    elif result.state == "launch_unknown":
        messages.error(
            request,
            "Не удалось подтвердить запуск AI-анализа; автоматический повтор заблокирован.",
        )
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
        messages.info(request, "Итерация первичного анализа не найдена.")
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
    else:
        messages.info(request, "AI-анализ ещё не был запущен.")
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
