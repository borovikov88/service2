from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from pool_service.communication_models import CallAnalysis
from pool_service.models import OrganizationAccess, ServiceTask, ServiceTaskChange
from pool_service.services.notifications import notify_task_assignment


MATERIALIZED_CONFIDENCE = "high"
ACTOR_EMPLOYEE = "employee"
ACTOR_CLIENT = "client"


def _communication_zone():
    zone_name = getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul")
    try:
        return ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _local_call_date(call):
    started_at = call.started_at
    if timezone.is_aware(started_at):
        return started_at.astimezone(_communication_zone()).date()
    return started_at.date()


def _valid_org_user(user, organization_id):
    if not user or not user.is_active:
        return False
    return OrganizationAccess.objects.filter(
        organization_id=organization_id,
        user_id=user.id,
    ).exists()


def _resolve_responsible(call):
    if _valid_org_user(call.employee, call.organization_id):
        return call.employee

    client = call.client
    crm_profile = getattr(client, "crm_profile", None) if client else None
    if crm_profile:
        for candidate in (crm_profile.responsible, crm_profile.manager):
            if _valid_org_user(candidate, call.organization_id):
                return candidate
    return None


def _parse_due_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_due_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").time()
    except (TypeError, ValueError):
        return None


def _due_at(due_date, due_time):
    if not due_date or not due_time:
        return None
    return datetime.combine(due_date, due_time).replace(tzinfo=_communication_zone())


def _task_description(call, commitment):
    actor = commitment.get("actor")
    lines = [
        "Автоматически выделено из расшифровки телефонного разговора.",
        (
            "Ожидаем действие клиента."
            if actor == ACTOR_CLIENT
            else "Обязательство сотрудника перед клиентом."
        ),
    ]
    evidence = str(commitment.get("evidence") or "").strip()
    if evidence:
        lines.append(f"Основание: {evidence}")
    if not commitment.get("due_date"):
        lines.append("Срок в разговоре явно не определён.")
    lines.append(f"Источник: звонок #{call.id}.")
    return "\n".join(lines)


def _task_title(commitment):
    action = str(commitment.get("action") or "").strip()
    if commitment.get("actor") == ACTOR_CLIENT:
        action = f"Ждём клиента: {action}"
    return action[:255]


def _existing_task(call, commitment_index):
    return ServiceTask.objects.filter(
        organization_id=call.organization_id,
        task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
        payload_json__source_call_id=call.id,
        payload_json__commitment_index=commitment_index,
    ).first()


def materialize_call_commitments(call_id):
    """Create CRM follow-up tasks for high-confidence commitments from a ready call analysis."""
    created_tasks = []

    with transaction.atomic():
        analysis = (
            CallAnalysis.objects.select_for_update()
            .select_related(
                "call",
                "call__employee",
                "call__client",
                "call__client__crm_profile__responsible",
                "call__client__crm_profile__manager",
            )
            .filter(call_id=call_id, status=CallAnalysis.STATUS_READY)
            .first()
        )
        if not analysis:
            return []

        facts = analysis.facts if isinstance(analysis.facts, dict) else {}
        commitments = facts.get("commitments")
        if not isinstance(commitments, list) or not commitments:
            return []

        call = analysis.call
        responsible = _resolve_responsible(call)
        if not responsible:
            return []

        for index, commitment in enumerate(commitments):
            if not isinstance(commitment, dict):
                continue

            actor = str(commitment.get("actor") or "").strip().lower()
            confidence = str(commitment.get("confidence") or "").strip().lower()
            action = str(commitment.get("action") or "").strip()

            if actor not in {ACTOR_EMPLOYEE, ACTOR_CLIENT}:
                continue
            if confidence != MATERIALIZED_CONFIDENCE:
                continue
            if not action:
                continue
            if _existing_task(call, index):
                continue

            due_date = _parse_due_date(commitment.get("due_date"))
            due_time = _parse_due_time(commitment.get("due_time"))
            task_date = due_date or _local_call_date(call)
            status = (
                ServiceTask.STATUS_WAITING
                if actor == ACTOR_CLIENT
                else ServiceTask.STATUS_NEW
            )

            task = ServiceTask.objects.create(
                organization=call.organization,
                title=_task_title(commitment),
                description=_task_description(call, commitment),
                start_date=task_date,
                end_date=due_date,
                start_time=due_time,
                end_time=due_time,
                task_type=ServiceTask.TYPE_CRM_FOLLOWUP,
                source_type=ServiceTask.SOURCE_SYSTEM,
                status=status,
                visibility=ServiceTask.VISIBILITY_PRIVATE,
                priority=ServiceTask.PRIORITY_NORMAL,
                client=call.client,
                created_by=None,
                primary_responsible=responsible,
                auto_created=True,
                is_editable=True,
                due_at=_due_at(due_date, due_time),
                payload_json={
                    "source": "call_analysis",
                    "source_call_id": call.id,
                    "commitment_index": index,
                    "actor": actor,
                    "kind": commitment.get("kind") or "other",
                    "confidence": confidence,
                    "evidence": commitment.get("evidence") or "",
                    "needs_due_date": due_date is None,
                },
            )
            task.responsibles.add(responsible)
            ServiceTaskChange.objects.create(
                task=task,
                changed_by=None,
                action=ServiceTaskChange.ACTION_CREATED,
                new_value=task.title,
            )
            created_tasks.append(task)

    for task in created_tasks:
        payload = task.payload_json if isinstance(task.payload_json, dict) else {}
        if payload.get("actor") == ACTOR_EMPLOYEE:
            notify_task_assignment(task, [task.primary_responsible], added_by=None)

    return created_tasks
