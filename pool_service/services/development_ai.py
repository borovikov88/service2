import logging
from dataclasses import dataclass
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from openai import OpenAI

from pool_service.models import (
    DevelopmentIteration,
    DevelopmentTask,
    DevelopmentTaskEvent,
)


logger = logging.getLogger(__name__)

PROVIDER = "openai"
PRIMARY_ANALYSIS_PURPOSE = "primary_analysis"
STATE_LAUNCHING = "launching"
STATE_QUEUED = "queued"
STATE_IN_PROGRESS = "in_progress"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_INCOMPLETE = "incomplete"
STATE_LAUNCH_UNKNOWN = "launch_unknown"
NONTERMINAL_STATES = {STATE_QUEUED, STATE_IN_PROGRESS}
REMOTE_FAILURE_STATES = {STATE_FAILED, STATE_CANCELLED, STATE_INCOMPLETE}
CREATE_MAX_RETRIES = 0
RETRIEVE_MAX_RETRIES = 2


class AnalysisConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class AnalysisOperationResult:
    state: str
    changed: bool = False
    message: str = ""


def _client(*, max_retries):
    if not settings.OPENAI_API_KEY:
        raise AnalysisConfigurationError("OpenAI integration is not configured")
    return OpenAI(
        api_key=settings.OPENAI_API_KEY,
        timeout=settings.OPENAI_DEVELOPMENT_TIMEOUT_SECONDS,
        max_retries=max_retries,
    )


def _now_iso():
    return timezone.now().isoformat()


def _metadata(iteration):
    value = iteration.automation_metadata
    return dict(value) if isinstance(value, dict) else {}


def _legacy_primary_iteration_id(task):
    iteration_ids = set()
    events = task.events.filter(
        event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED
    ).only("metadata")
    for event in events:
        metadata = event.metadata if isinstance(event.metadata, dict) else {}
        if metadata.get("action") != "start":
            continue
        try:
            iteration_ids.add(int(metadata["iteration_id"]))
        except (KeyError, TypeError, ValueError):
            return None
    if len(iteration_ids) != 1:
        return None
    return iteration_ids.pop()


def resolve_primary_analysis_iteration(task):
    marked = list(
        task.iterations.filter(
            executor_type=DevelopmentIteration.EXECUTOR_SYSTEM,
            automation_metadata__purpose=PRIMARY_ANALYSIS_PURPOSE,
        ).order_by("id")[:2]
    )
    if len(marked) == 1:
        return marked[0]
    if marked:
        return None

    legacy_id = _legacy_primary_iteration_id(task)
    if legacy_id is None:
        return None
    iteration = task.iterations.filter(
        pk=legacy_id,
        executor_type=DevelopmentIteration.EXECUTOR_SYSTEM,
    ).first()
    if iteration is None:
        return None
    metadata = _metadata(iteration)
    if metadata.get("purpose") not in {None, "", PRIMARY_ANALYSIS_PURPOSE}:
        return None
    return iteration


def _is_primary_analysis_iteration(iteration):
    primary = resolve_primary_analysis_iteration(iteration.task)
    return primary is not None and primary.pk == iteration.pk


def _promote_legacy_primary_iteration(iteration):
    metadata = _metadata(iteration)
    if metadata.get("purpose") == PRIMARY_ANALYSIS_PURPOSE:
        return metadata
    metadata["purpose"] = PRIMARY_ANALYSIS_PURPOSE
    iteration.automation_metadata = metadata
    iteration.save(update_fields=["automation_metadata", "updated_at"])
    return metadata


def _summary(text, limit=500):
    compact = " ".join((text or "").split())
    if not compact:
        return "Первичный технический анализ завершён."
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def _create_background_response(iteration, launch_token):
    return _client(max_retries=CREATE_MAX_RETRIES).responses.create(
        model=settings.OPENAI_DEVELOPMENT_MODEL,
        background=True,
        max_output_tokens=settings.OPENAI_DEVELOPMENT_MAX_OUTPUT_TOKENS,
        instructions=(
            "Подготовь только итоговый технический анализ задачи на русском языке. "
            "Не раскрывай chain-of-thought или скрытые рассуждения. Структурируй ответ: "
            "понимание задачи; технический контекст; предполагаемые части приложения; "
            "риски; безопасность, права и целостность данных; план реализации; "
            "проверка Definition of Done; рекомендации для Codex. Не выполняй действий "
            "во внешних системах и production."
        ),
        input=iteration.prompt,
        metadata={
            "purpose": "development_primary_analysis",
            "task_id": str(iteration.task_id),
            "iteration_id": str(iteration.pk),
            "launch_token": launch_token,
        },
    )


def _retrieve_response(response_id):
    return _client(max_retries=RETRIEVE_MAX_RETRIES).responses.retrieve(response_id)


def _record_launch_failure(iteration_id, launch_token):
    with transaction.atomic():
        iteration = (
            DevelopmentIteration.objects.select_for_update()
            .select_related("task")
            .get(pk=iteration_id)
        )
        metadata = _metadata(iteration)
        if metadata.get("launch_token") != launch_token or metadata.get("response_id"):
            return
        metadata.update(
            {
                "provider": PROVIDER,
                "state": STATE_LAUNCH_UNKNOWN,
                "last_error": "Не удалось подтвердить создание AI-анализа.",
                "updated_at": _now_iso(),
            }
        )
        iteration.automation_metadata = metadata
        iteration.status = DevelopmentIteration.STATUS_FAILED
        iteration.technical_errors = (
            "Не удалось подтвердить запуск AI-анализа. Автоматический повтор отключён, "
            "чтобы не создать дублирующий платный запрос."
        )
        iteration.completed_at = timezone.now()
        iteration.save(
            update_fields=[
                "automation_metadata",
                "status",
                "technical_errors",
                "completed_at",
                "updated_at",
            ]
        )
        task = iteration.task
        old_status = task.status
        task.status = DevelopmentTask.STATUS_BLOCKED
        task.current_stage = DevelopmentTask.STAGE_ANALYSIS
        task.current_activity = "Запуск AI-анализа требует проверки"
        task.blockers = iteration.technical_errors
        task.save(
            update_fields=[
                "status",
                "current_stage",
                "current_activity",
                "blockers",
                "updated_at",
            ]
        )
        DevelopmentTaskEvent.objects.create(
            task=task,
            event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
            message="Не удалось подтвердить запуск AI-анализа",
            metadata={
                "old_status": old_status,
                "new_status": task.status,
                "iteration_id": iteration.pk,
                "iteration_number": iteration.iteration_number,
                "action": "ai_analysis_launch_unknown",
            },
        )


def _apply_response(iteration_id, response):
    remote_state = str(getattr(response, "status", "") or "").lower()
    with transaction.atomic():
        iteration = (
            DevelopmentIteration.objects.select_for_update()
            .select_related("task")
            .get(pk=iteration_id)
        )
        metadata = _metadata(iteration)
        if not _is_primary_analysis_iteration(iteration):
            return AnalysisOperationResult("not_available", changed=False)
        if metadata.get("applied"):
            return AnalysisOperationResult(STATE_COMPLETED, changed=False)
        if iteration.status != DevelopmentIteration.STATUS_WORKING:
            return AnalysisOperationResult("task_state_changed", changed=False)
        if metadata.get("response_id") != getattr(response, "id", None):
            raise RuntimeError("Response does not belong to this iteration")

        task = iteration.task
        metadata.update(
            {
                "provider": PROVIDER,
                "state": remote_state,
                "checked_at": _now_iso(),
            }
        )
        if task.status != DevelopmentTask.STATUS_ANALYSIS:
            iteration.automation_metadata = metadata
            iteration.save(update_fields=["automation_metadata", "updated_at"])
            return AnalysisOperationResult("task_state_changed", changed=True)

        if remote_state in NONTERMINAL_STATES:
            iteration.automation_metadata = metadata
            iteration.save(update_fields=["automation_metadata", "updated_at"])
            task.current_activity = "AI выполняет первичный анализ"
            task.save(update_fields=["current_activity", "updated_at"])
            return AnalysisOperationResult(remote_state, changed=True)

        if remote_state == STATE_COMPLETED:
            output_text = str(getattr(response, "output_text", "") or "").strip()
            if not output_text:
                remote_state = STATE_INCOMPLETE
                metadata["state"] = remote_state
            else:
                now = timezone.now()
                metadata.update({"applied": True, "completed_at": now.isoformat()})
                iteration.automation_metadata = metadata
                iteration.response = output_text
                iteration.result_summary = _summary(output_text)
                iteration.status = DevelopmentIteration.STATUS_ACCEPTED
                iteration.technical_errors = ""
                iteration.completed_at = now
                iteration.save(
                    update_fields=[
                        "automation_metadata",
                        "response",
                        "result_summary",
                        "status",
                        "technical_errors",
                        "completed_at",
                        "updated_at",
                    ]
                )
                old_status = task.status
                task.status = DevelopmentTask.STATUS_READY_FOR_CODEX
                task.current_stage = DevelopmentTask.STAGE_DEVELOPMENT
                task.completed_work = "Выполнен первичный технический AI-анализ задачи."
                task.current_activity = "Первичный AI-анализ завершён; задача готова к передаче Codex"
                task.blockers = ""
                task.save(
                    update_fields=[
                        "status",
                        "current_stage",
                        "completed_work",
                        "current_activity",
                        "blockers",
                        "updated_at",
                    ]
                )
                DevelopmentTaskEvent.objects.create(
                    task=task,
                    event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
                    message="Первичный AI-анализ завершён",
                    metadata={
                        "old_status": old_status,
                        "new_status": task.status,
                        "iteration_id": iteration.pk,
                        "iteration_number": iteration.iteration_number,
                        "action": "ai_analysis_completed",
                    },
                )
                return AnalysisOperationResult(STATE_COMPLETED, changed=True)

        if remote_state not in REMOTE_FAILURE_STATES:
            remote_state = STATE_FAILED
            metadata["state"] = remote_state
        now = timezone.now()
        message = {
            STATE_CANCELLED: "AI-анализ был отменён провайдером.",
            STATE_INCOMPLETE: "AI-анализ завершился без пригодного результата.",
        }.get(remote_state, "AI-анализ завершился ошибкой провайдера.")
        metadata.update({"applied": True, "completed_at": now.isoformat()})
        iteration.automation_metadata = metadata
        iteration.status = (
            DevelopmentIteration.STATUS_CANCELLED
            if remote_state == STATE_CANCELLED
            else DevelopmentIteration.STATUS_FAILED
        )
        iteration.technical_errors = message
        iteration.completed_at = now
        iteration.save(
            update_fields=[
                "automation_metadata",
                "status",
                "technical_errors",
                "completed_at",
                "updated_at",
            ]
        )
        old_status = task.status
        task.status = DevelopmentTask.STATUS_BLOCKED
        task.current_stage = DevelopmentTask.STAGE_ANALYSIS
        task.current_activity = "Первичный AI-анализ не завершён"
        task.blockers = message
        task.save(
            update_fields=[
                "status",
                "current_stage",
                "current_activity",
                "blockers",
                "updated_at",
            ]
        )
        DevelopmentTaskEvent.objects.create(
            task=task,
            event_type=DevelopmentTaskEvent.TYPE_STATUS_CHANGED,
            message=message,
            metadata={
                "old_status": old_status,
                "new_status": task.status,
                "iteration_id": iteration.pk,
                "iteration_number": iteration.iteration_number,
                "action": "ai_analysis_failed",
                "provider_state": remote_state,
            },
        )
        return AnalysisOperationResult(remote_state, changed=True, message=message)


def launch_analysis(iteration_id):
    if not settings.OPENAI_API_KEY:
        return AnalysisOperationResult("not_configured", message="OpenAI integration is not configured")

    launch_token = uuid4().hex
    with transaction.atomic():
        iteration = (
            DevelopmentIteration.objects.select_for_update()
            .select_related("task")
            .get(pk=iteration_id)
        )
        metadata = _metadata(iteration)
        if (
            iteration.executor_type != DevelopmentIteration.EXECUTOR_SYSTEM
            or iteration.status != DevelopmentIteration.STATUS_WORKING
            or iteration.task.status != DevelopmentTask.STATUS_ANALYSIS
            or not _is_primary_analysis_iteration(iteration)
        ):
            return AnalysisOperationResult("not_available", changed=False)
        metadata = _promote_legacy_primary_iteration(iteration)
        if metadata.get("response_id"):
            return AnalysisOperationResult(metadata.get("state", STATE_QUEUED), changed=False)
        if metadata.get("state") in {STATE_LAUNCHING, STATE_LAUNCH_UNKNOWN}:
            return AnalysisOperationResult(metadata["state"], changed=False)
        metadata.update(
            {
                "provider": PROVIDER,
                "state": STATE_LAUNCHING,
                "model": settings.OPENAI_DEVELOPMENT_MODEL,
                "launch_token": launch_token,
                "launch_started_at": _now_iso(),
            }
        )
        iteration.automation_metadata = metadata
        iteration.save(update_fields=["automation_metadata", "updated_at"])
        task = iteration.task
        task.current_activity = "AI-анализ запускается"
        task.save(update_fields=["current_activity", "updated_at"])

    try:
        response = _create_background_response(iteration, launch_token)
        response_id = str(getattr(response, "id", "") or "").strip()
        if not response_id:
            raise RuntimeError("Provider response has no identifier")
    except Exception as exc:
        logger.warning(
            "Development AI launch outcome unknown: task=%s iteration=%s error_type=%s",
            iteration.task_id,
            iteration.pk,
            type(exc).__name__,
        )
        _record_launch_failure(iteration.pk, launch_token)
        return AnalysisOperationResult(STATE_LAUNCH_UNKNOWN, changed=True)

    with transaction.atomic():
        locked = (
            DevelopmentIteration.objects.select_for_update()
            .select_related("task")
            .get(pk=iteration.pk)
        )
        metadata = _metadata(locked)
        if metadata.get("launch_token") != launch_token:
            raise RuntimeError("Analysis launch ownership changed unexpectedly")
        existing_id = metadata.get("response_id")
        if existing_id and existing_id != response_id:
            raise RuntimeError("A different response is already attached")
        metadata.update(
            {
                "response_id": response_id,
                "state": str(getattr(response, "status", "queued") or "queued").lower(),
                "response_saved_at": _now_iso(),
            }
        )
        locked.automation_metadata = metadata
        locked.save(update_fields=["automation_metadata", "updated_at"])
        task = locked.task
        if task.status == DevelopmentTask.STATUS_ANALYSIS:
            task.current_activity = "AI выполняет первичный анализ"
            task.save(update_fields=["current_activity", "updated_at"])

    state = str(getattr(response, "status", "queued") or "queued").lower()
    if state not in NONTERMINAL_STATES:
        return _apply_response(iteration.pk, response)
    return AnalysisOperationResult(state, changed=True)


def check_analysis(iteration_id):
    with transaction.atomic():
        iteration = (
            DevelopmentIteration.objects.select_for_update()
            .select_related("task")
            .get(pk=iteration_id)
        )
        metadata = _metadata(iteration)
        if not _is_primary_analysis_iteration(iteration):
            return AnalysisOperationResult("not_available", changed=False)
        if metadata.get("applied"):
            return AnalysisOperationResult(metadata.get("state", STATE_COMPLETED), changed=False)
        if (
            iteration.executor_type != DevelopmentIteration.EXECUTOR_SYSTEM
            or iteration.status != DevelopmentIteration.STATUS_WORKING
            or iteration.task.status != DevelopmentTask.STATUS_ANALYSIS
        ):
            return AnalysisOperationResult("not_available", changed=False)
        response_id = metadata.get("response_id")
        if not response_id:
            return AnalysisOperationResult(metadata.get("state", "not_started"), changed=False)

    try:
        response = _retrieve_response(response_id)
    except Exception as exc:
        logger.warning(
            "Development AI status check failed: task=%s iteration=%s error_type=%s",
            iteration.task_id,
            iteration.pk,
            type(exc).__name__,
        )
        with transaction.atomic():
            locked = DevelopmentIteration.objects.select_for_update().get(pk=iteration.pk)
            metadata = _metadata(locked)
            if not metadata.get("applied"):
                metadata.update(
                    {
                        "last_check_error": "Не удалось проверить состояние AI-анализа.",
                        "checked_at": _now_iso(),
                    }
                )
                locked.automation_metadata = metadata
                locked.save(update_fields=["automation_metadata", "updated_at"])
        return AnalysisOperationResult("check_failed", changed=True)
    return _apply_response(iteration.pk, response)
