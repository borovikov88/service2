"""Authorized commercial commands; HTTP adapters never write the models directly."""
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
import re
import hashlib
import json
from zoneinfo import ZoneInfo

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import router, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.crm_commercial_models import (
    CANONICAL_KEY, CrmAccess, CrmCommand, CrmDeal, CrmDealNextAction,
    CrmDealTaskLink, CrmEvent, CrmLead, CrmPipeline, CrmStage,
)
from pool_service.models import Client, Employee, Organization, OrganizationAccess, ServiceTask, ServiceTaskChange


class CommercialConflict(ValidationError):
    """The key was reused or the caller's subject revision is stale."""


CARD_FIELDS = {"work_subject", "specialization", "fulfillment_kind", "object_kind", "pool_id", "client_object_id",
    "planned_object_name", "planned_object_address", "amount", "currency", "amount_basis", "expected_decision_at"}

COMMANDS = {
    "lead.edit": (CrmLead, "assignee_id", "edit_own_leads", {"title", "need"}),
    "lead.transition": (CrmLead, "assignee_id", "transition_own_leads", {"status", "reason", "follow_up_at"}),
    "deal.edit": (CrmDeal, "manager_id", "edit_own_deals", {"title"} | CARD_FIELDS),
    "deal.transition": (CrmDeal, "manager_id", "transition_own_deals", {"stage_id", "reason"}),
}
# Explicit technical bounds, independent of editable labels/positions. No tender
# automation, contract, sending, execution, payments or closure in this release.
PREPARATORY_STAGES = {
    "construction": ("need", "technical", "proposal"),
    "service": ("request", "assessment", "proposal"),
    "retail": ("need", "selection", "proposal"),
    "tender": ("review", "assessment", "preparation"),
}
LEAD_STATES = ("new", "working", "qualifying", "waiting", "deferred")


def _positive_id(value):
    if type(value) is not int or value <= 0:
        raise ValidationError("CRM identifiers must be positive integers.")
    return value


def _normalize(command_type, changes):
    if type(command_type) is not str or command_type not in COMMANDS or type(changes) is not dict:
        raise ValidationError("Unknown CRM command or payload.")
    allowed = COMMANDS[command_type][3]
    if not changes or not changes.keys() <= allowed:
        raise ValidationError("Fields are unavailable for this CRM command.")
    normalized = dict(changes)
    for key, value in normalized.items():
        if key == "stage_id":
            _positive_id(value)
        elif key in {"pool_id", "client_object_id"}:
            if value is not None:
                _positive_id(value)
        elif key == "amount":
            if value is not None:
                if type(value) not in (str, Decimal) or (type(value) is str and len(value) > 40):
                    raise ValidationError("Сумму укажите десятичной строкой.")
                try:
                    amount = Decimal(value)
                except InvalidOperation as exc:
                    raise ValidationError("Некорректная сумма.") from exc
                if (not amount.is_finite() or amount < 0 or amount >= Decimal("10000000000000000")
                        or amount.as_tuple().exponent < -2):
                    raise ValidationError("Сумма должна быть неотрицательной, до 16 цифр и 2 знаков после запятой.")
                normalized[key] = format(abs(amount) if amount.is_zero() else amount, ".2f")
        elif key in {"follow_up_at", "expected_decision_at"}:
            if value is not None:
                if isinstance(value, str):
                    try:
                        value = parse_datetime(value)
                    except ValueError as exc:
                        raise ValidationError("Invalid follow-up datetime.") from exc
                if not isinstance(value, datetime) or timezone.is_naive(value):
                    raise ValidationError("Follow-up requires a timezone-aware datetime.")
                try:
                    normalized[key] = value.astimezone(datetime_timezone.utc).isoformat()
                except (OverflowError, ValueError) as exc:
                    raise ValidationError("Follow-up datetime is outside the supported UTC range.") from exc
        elif type(value) is not str:
            raise ValidationError("CRM text must be a string.")
        else:
            normalized[key] = value.strip()
            limit = {"title": 255, "need": 10000, "reason": 500, "work_subject": 2000, "specialization": 40, "fulfillment_kind": 24, "planned_object_name": 255, "planned_object_address": 500, "amount_basis": 24, "currency": 3}.get(key, 16)
            if len(normalized[key]) > limit or (key == "title" and not normalized[key]):
                raise ValidationError("CRM text is empty or too long.")
    if "currency" in normalized and normalized["currency"] and not re.fullmatch(r"[A-Z]{3}", normalized["currency"]):
        raise ValidationError("Валюта — три заглавные латинские буквы.")
    if command_type.endswith(".transition"):
        normalized.setdefault("reason", "")
    if command_type == "lead.transition":
        normalized.setdefault("follow_up_at", None)
    if command_type == "lead.transition" and "status" not in normalized:
        raise ValidationError("A lead transition requires status.")
    if command_type == "deal.transition" and "stage_id" not in normalized:
        raise ValidationError("A deal transition requires stage_id.")
    return normalized


def _authorize(using, actor_id, organization, capability):
    # Current locking reads are deliberate: never authorize from passed instances
    # or a MySQL repeatable-read snapshot. Locks are retained through replay too.
    actor = get_user_model().objects.using(using).select_for_update().filter(pk=actor_id).first()
    memberships = list(OrganizationAccess.objects.using(using).select_for_update().filter(
        user_id=actor_id, organization_id=organization.pk).order_by("pk"))
    employees = list(Employee.objects.using(using).select_for_update().filter(
        user_id=actor_id, organization_id=organization.pk).order_by("pk"))
    grant = CrmAccess.objects.using(using).select_for_update().filter(
        user_id=actor_id, organization_id=organization.pk).first()
    capabilities = (capability,) if isinstance(capability, str) else tuple(capability)
    if (not actor or not actor.is_active or not memberships
            or not organization.crm_v2_read or not organization.crm_v2_write
            or any(not employee.is_active or employee.employment_status == Employee.STATUS_DISMISSED for employee in employees)
            or not grant or not all(getattr(grant, name) for name in capabilities)
            or not (any(item.role == "manager" for item in memberships) or grant.view_team_deals)):
        raise PermissionDenied("Current CRM command access is unavailable.")
    return actor


def _card_value(key, value):
    if key == "amount" and value is not None:
        return format(Decimal(value), ".2f")
    if key == "expected_decision_at" and value is not None:
        return value.astimezone(datetime_timezone.utc).isoformat()
    return value


def _set_card_fields(subject, changes, diff):
    changed = False
    for key, value in changes.items():
        old = _card_value(key, getattr(subject, key))
        if old == value:
            continue
        changed = True
        if key == "amount" and value is not None:
            setattr(subject, key, Decimal(value))
        elif key == "expected_decision_at" and value is not None:
            setattr(subject, key, parse_datetime(value))
        else:
            setattr(subject, key, value)
        if key in {"title", "work_subject", "specialization"}:
            diff[key + "_changed"] = {"before": False, "after": True}
        elif key in {"planned_object_name", "planned_object_address"}:
            diff["planned_object_changed"] = {"before": False, "after": True}
        else:
            diff[key] = {"before": old, "after": value}
    return changed


def _apply(subject, command_type, changes, using, actor=None):
    before = subject.revision
    diff = {"revision": {"before": before, "after": before + 1}}
    reason = changes.get("reason", "")
    if subject.is_archived:
        raise ValidationError("Archived CRM subjects are read-only.")
    if isinstance(subject, CrmLead):
        if subject.status not in LEAD_STATES or subject.result:
            raise ValidationError("Completed lead qualification is outside this release.")
        if command_type == "lead.transition":
            status = changes["status"]
            if status not in LEAD_STATES or status == subject.status or status == "new":
                raise ValidationError("This lead transition is unavailable.")
            if LEAD_STATES.index(status) < LEAD_STATES.index(subject.status) and not reason:
                raise ValidationError("A backward transition requires a reason.")
            follow_up = changes.get("follow_up_at")
            if status in {"waiting", "deferred"} and (not reason or not follow_up):
                raise ValidationError("Waiting or deferred leads require a reason and check date.")
            diff["status"] = {"before": subject.status, "after": status}
            subject.status, subject.reason = status, reason
            subject.follow_up_at = parse_datetime(follow_up) if follow_up else None
    else:
        if subject.outcome != "open":
            raise ValidationError("Only open deals are supported.")
        # Always validate the full current graph, including on an edit.
        pipeline = CrmPipeline.objects.using(using).select_for_update().get(pk=subject.pipeline_id)
        stages = {stage.pk: stage for stage in CrmStage.objects.using(using).select_for_update().filter(
            pk__in={subject.stage_id, changes.get("stage_id", subject.stage_id)}).order_by("pk")}
        current = stages[subject.stage_id]
        if pipeline.organization_id != subject.organization_id or current.pipeline_id != pipeline.pk:
            raise ValidationError("CRM stage/pipeline scope is inconsistent.")
        if command_type == "deal.transition":
            target = stages.get(changes["stage_id"])
            order = PREPARATORY_STAGES.get(pipeline.code, ())
            if not target or target.pipeline_id != pipeline.pk:
                raise ValidationError("Target stage must belong to the current pipeline.")
            if not pipeline.is_active or not target.is_active:
                raise ValidationError("The target pipeline or stage is disabled.")
            if current.code not in order or target.code not in order:
                raise ValidationError("Stage requires a later contract, execution, payment or closure process.")
            if target.pk == current.pk:
                raise ValidationError("The deal is already at this stage.")
            if order.index(target.code) < order.index(current.code) and not reason:
                raise ValidationError("A backward transition requires a reason.")
            diff["stage_id"] = {"before": subject.stage_id, "after": target.pk}
            subject.stage = target
    if command_type.endswith(".edit"):
        changed = _set_card_fields(subject, changes, diff) if isinstance(subject, CrmDeal) else False
        if isinstance(subject, CrmLead):
            for key, value in changes.items():
                if getattr(subject, key) != value:
                    changed = True
                    setattr(subject, key, value)
                    diff[f"{key}_changed"] = {"before": False, "after": True}
        if not changed:
            raise ValidationError("The command has no changes.")
    if isinstance(subject, CrmDeal):
        from .crm_deal_objects import validate_deal_object
        validate_deal_object(using, actor, subject)
    subject.revision += 1
    subject.save(using=using)
    return diff, reason


def execute_commercial_command(*, actor, organization, operation_key, command_type,
                               subject_id, expected_revision, changes):
    """Apply or replay one authorized command; return structural IDs/revision.

    The caller supplies account/org identity only, never trusted cached rights.
    Failed commands leave no durable operation row and may be retried after repair.
    """
    actor_id = _positive_id(getattr(actor, "pk", None))
    organization_id = _positive_id(getattr(organization, "pk", None))
    _positive_id(subject_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValidationError("expected_revision must be a nonnegative integer.")
    if type(operation_key) is not str or len(operation_key) > 100:
        raise ValidationError("Invalid operation key.")
    CANONICAL_KEY(operation_key)  # no lowercasing/trimming aliases, even on MySQL
    changes = _normalize(command_type, changes)
    model, ownership, capability, _fields = COMMANDS[command_type]
    payload = {"actor_id": actor_id, "organization_id": organization_id,
               "command_type": command_type, "subject_id": subject_id,
               "expected_revision": expected_revision, "changes": changes}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    using = router.db_for_write(CrmCommand)
    with transaction.atomic(using=using):
        try:
            organization = Organization.objects.using(using).select_for_update().get(pk=organization_id)
        except Organization.DoesNotExist as exc:
            raise PermissionDenied("Organization unavailable.") from exc
        actor = _authorize(using, actor_id, organization, capability)
        try:
            subject = model.objects.using(using).select_for_update().get(pk=subject_id, organization_id=organization_id)
        except model.DoesNotExist as exc:
            raise PermissionDenied("CRM subject unavailable.") from exc
        if isinstance(subject, CrmLead) and (subject.next_task_id or subject.sources.exists() or subject.threads.exists()):
            raise ValidationError("Use execute_lead_command for intake leads.")
        if getattr(subject, ownership) != actor_id:
            raise PermissionDenied("Only the actor's own CRM cards may be changed.")
        # Lock a current legacy client link as well. This does not turn legacy
        # writers into command writers or promise a durable tenant CHECK.
        if subject.client_id:
            from pool_service.models import Client
            client = Client.objects.using(using).select_for_update().get(pk=subject.client_id)
            if client.organization_id != organization_id:
                raise ValidationError("CRM client scope is inconsistent.")
            if isinstance(subject, CrmDeal):
                from pool_service.client_queries import active_clients
                if not active_clients(Client.objects.using(using)).filter(pk=client.pk).exists():
                    raise PermissionDenied("Deal client is no longer an active CRM identity.")
        # Revalidate tenant links before returning even a saved result.
        subject.full_clean()
        if isinstance(subject, CrmDeal):
            from .crm_deal_objects import validate_deal_object
            validate_deal_object(using, actor, subject)
        previous = CrmCommand.objects.using(using).select_for_update().filter(
            organization_id=organization_id, operation_key=operation_key).first()
        if previous:
            if previous.actor_id != actor_id or previous.command_type != command_type or previous.payload_hash != digest:
                raise CommercialConflict("Operation key was used with different content.")
            return dict(previous.result)
        if subject.revision != expected_revision:
            raise CommercialConflict("CRM revision changed; reload the card.")
        diff, reason = _apply(subject, command_type, changes, using, actor)
        command = CrmCommand.objects.using(using).create(
            organization=organization, operation_key=operation_key, command_type=command_type,
            payload_hash=digest, actor_id=actor_id)
        event = CrmEvent.objects.using(using).create(
            organization=organization, **{"lead" if model is CrmLead else "deal": subject},
            command=command, actor_id=actor_id, event_type=command_type, safe_diff=diff, reason=reason)
        command.result = {"lead_id" if model is CrmLead else "deal_id": subject.pk,
                          "event_id": event.pk, "revision": subject.revision}
        command.save(using=using, update_fields=["result"])
        return dict(command.result)


def _next_action_values(value):
    if type(value) is not dict or set(value) != {"title", "due_at"}:
        raise ValidationError("Укажите название и дату следующего действия.")
    title = _normalize("deal.edit", {"title": value["title"]})["title"]
    due = value["due_at"]
    if isinstance(due, str):
        try:
            due = parse_datetime(due)
        except ValueError as exc:
            raise ValidationError("Некорректная дата следующего действия.") from exc
    if not isinstance(due, datetime) or timezone.is_naive(due):
        raise ValidationError("Дата следующего действия должна содержать часовой пояс.")
    try:
        due = due.astimezone(datetime_timezone.utc)
        due.astimezone(ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul")))
    except (OverflowError, ValueError) as exc:
        raise ValidationError("Дата следующего действия вне допустимого диапазона.") from exc
    return {"title": title, "due_at": due.isoformat()}


def _pilot_payload(*, actor, organization, operation_key, command_type, **values):
    actor_id = _positive_id(getattr(actor, "pk", None))
    organization_id = _positive_id(getattr(organization, "pk", None))
    if type(operation_key) is not str or len(operation_key) > 100:
        raise ValidationError("Invalid operation key.")
    CANONICAL_KEY(operation_key)
    payload = dict(actor_id=actor_id, organization_id=organization_id,
                   command_type=command_type, **values)
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return actor_id, organization_id, digest


def _locked_organization(using, organization_id):
    organization = Organization.objects.using(using).select_for_update().filter(pk=organization_id).first()
    if not organization:
        raise PermissionDenied("Organization unavailable.")
    return organization


def _previous_pilot_command(using, organization_id, operation_key):
    return CrmCommand.objects.using(using).select_for_update().filter(
        organization_id=organization_id, operation_key=operation_key).first()


def _locked_pilot_tasks(using, *, pointer=None, previous=None):
    # The organization mutex serializes all supported pointer writers. Lock the
    # existing task BEFORE actor rows: task feedback uses task -> actor too.
    # New tasks are not visible to another transaction until their creation ends.
    task_ids = set()
    if pointer:
        task_ids.add(pointer.task_link.task_id)
    if previous and type(previous.result.get("task_id")) is int:
        task_ids.add(previous.result["task_id"])
    return {task.pk: task for task in ServiceTask.objects.using(using).select_for_update().filter(pk__in=task_ids).order_by("pk")}


def _owned_deal(using, actor_id, organization_id, subject_id):
    deal = CrmDeal.objects.using(using).select_for_update().filter(
        pk=subject_id, organization_id=organization_id, manager_id=actor_id).first()
    if not deal:
        raise PermissionDenied("Only the actor's own CRM cards may be changed.")
    if deal.client_id:
        from pool_service.client_queries import active_clients
        client = active_clients(Client.objects.using(using).select_for_update()).filter(pk=deal.client_id).first()
        if not client or client.organization_id != organization_id:
            raise ValidationError("CRM client scope is inconsistent.")
    deal.full_clean()
    from .crm_deal_objects import validate_deal_object
    from django.contrib.auth import get_user_model
    validate_deal_object(using, get_user_model().objects.using(using).get(pk=actor_id), deal)
    return deal


def _validate_task_link(using, actor, deal, task):
    from pool_service.services.call_privacy import private_source_task_ids
    from pool_service.services.task_feedback_permissions import lock_feedback_actor
    link = CrmDealTaskLink.objects.using(using).filter(
        task_id=getattr(task, "pk", None), deal_id=deal.pk, organization_id=deal.organization_id).first()
    if not task or not link:
        raise PermissionDenied("Следующее действие недоступно.")
    link.full_clean()
    if task.source_type != ServiceTask.SOURCE_MANAGER or private_source_task_ids([task]):
        raise PermissionDenied("Следующее действие недоступно.")
    lock_feedback_actor(task, actor)
    return link


def _replay_pilot(using, previous, actor, organization_id, digest, command_type, tasks):
    if (previous.actor_id != actor.pk or previous.command_type != command_type
            or previous.payload_hash != digest):
        raise CommercialConflict("Operation key was used with different content.")
    deal = _owned_deal(using, actor.pk, organization_id, previous.result.get("deal_id"))
    task = tasks.get(previous.result.get("task_id"))
    _validate_task_link(using, actor, deal, task)
    return dict(previous.result)


def _new_next_task(using, actor, deal, values):
    due = parse_datetime(values["due_at"])
    local = due.astimezone(ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul")))
    task = ServiceTask.objects.using(using).create(
        organization_id=deal.organization_id, client_id=deal.client_id,
        created_by=actor, primary_responsible=actor, title=values["title"],
        task_type=ServiceTask.TYPE_CRM_FOLLOWUP, source_type=ServiceTask.SOURCE_MANAGER,
        visibility=ServiceTask.VISIBILITY_PRIVATE,
        start_date=local.date(), end_date=local.date(), start_time=local.time(), end_time=local.time(),
        due_at=due, payload_json={"crm_commercial": {"deal_id": deal.pk}},
    )
    task.responsibles.add(actor)
    link = CrmDealTaskLink.objects.using(using).create(
        organization_id=deal.organization_id, deal=deal, task=task, created_by=actor)
    ServiceTaskChange.objects.using(using).create(task=task, changed_by=actor,
        action=ServiceTaskChange.ACTION_CREATED, field_name="crm_next_action")
    return task, link


def _record_pilot_command(using, *, actor, deal, operation_key, digest, command_type, task, before):
    command = CrmCommand.objects.using(using).create(
        organization_id=deal.organization_id, operation_key=operation_key,
        command_type=command_type, payload_hash=digest, actor=actor)
    diff = {"revision": {"before": before, "after": deal.revision},
            "next_action_changed": {"before": False, "after": True}}
    if command_type == "deal.create":
        diff.update({name: {"before": None, "after": getattr(deal, name)}
                     for name in ("pipeline_id", "stage_id", "manager_id")})
    event = CrmEvent.objects.using(using).create(organization_id=deal.organization_id,
        deal=deal, command=command, actor=actor, event_type=command_type, safe_diff=diff)
    command.result = dict(deal_id=deal.pk, event_id=event.pk, revision=deal.revision, task_id=task.pk)
    command.save(using=using, update_fields=["result"])
    return dict(command.result)


def create_commercial_deal(*, actor, organization, operation_key, client_id, pipeline_id, title, next_action, card_fields=None, source_context=None):
    """Create an own preparatory deal and its first real task, or replay it."""
    from pool_service.client_queries import active_clients
    _positive_id(client_id)
    _positive_id(pipeline_id)
    title = _normalize("deal.edit", {"title": title})["title"]
    values = _next_action_values(next_action)
    if card_fields is None:
        card_fields = {}
    if type(card_fields) is not dict or not card_fields.keys() <= CARD_FIELDS:
        raise ValidationError("Некорректные поля карточки сделки.")
    card_fields = _normalize("deal.edit", card_fields) if card_fields else {}
    context_fields = {}
    if source_context is not None:
        from .crm_need_deals import NeedDealContext
        if type(source_context) is not NeedDealContext:
            raise ValidationError('Invalid internal source context.')
        if client_id != source_context.client_id or card_fields != {'object_kind': 'pool', 'pool_id': source_context.pool_id}:
            raise PermissionDenied('Source client/object mismatch.')
        context_fields = {'source_context': source_context.payload}
    actor_id, organization_id, digest = _pilot_payload(actor=actor, organization=organization,
        operation_key=operation_key, command_type="deal.create", client_id=client_id,
        pipeline_id=pipeline_id, title=title, next_action=values, **({"card_fields": card_fields} if card_fields else {}), **context_fields)
    using = router.db_for_write(CrmCommand)
    with transaction.atomic(using=using):
        organization = _locked_organization(using, organization_id)
        previous = _previous_pilot_command(using, organization_id, operation_key)
        if source_context is not None:
            source_context.validate(actor_id=actor_id, organization_id=organization_id, using=using, previous=previous)
        tasks = _locked_pilot_tasks(using, previous=previous)
        actor = _authorize(using, actor_id, organization, ("create_own_deals", "manage_own_next_action"))
        if previous:
            return _replay_pilot(using, previous, actor, organization_id, digest, "deal.create", tasks)
        client = active_clients(Client.objects.using(using).select_for_update()).filter(
            pk=client_id, organization_id=organization_id).first()
        pipeline = CrmPipeline.objects.using(using).select_for_update().filter(pk=pipeline_id, organization_id=organization_id).first()
        if not client or not pipeline:
            raise PermissionDenied("Клиент или воронка недоступны в этой организации.")
        order = PREPARATORY_STAGES.get(pipeline.code, ())
        stage = CrmStage.objects.using(using).select_for_update().filter(
            pipeline=pipeline, code=order[0] if order else "", is_active=True).first()
        if not pipeline.is_active or not stage:
            raise ValidationError("Начальный этап этой воронки недоступен.")
        deal = CrmDeal(organization=organization, pipeline=pipeline, stage=stage,
            client=client, manager=actor, title=title, revision=1)
        _set_card_fields(deal, card_fields, {})
        from .crm_deal_objects import validate_deal_object
        validate_deal_object(using, actor, deal)
        deal.save(using=using)
        task, link = _new_next_task(using, actor, deal, values)
        CrmDealNextAction.objects.using(using).create(organization=organization, deal=deal, task_link=link)
        return _record_pilot_command(using, actor=actor, deal=deal, operation_key=operation_key,
            digest=digest, command_type="deal.create", task=task, before=0)


def set_deal_next_action(*, actor, organization, operation_key, subject_id, expected_revision,
                         title, due_at, expected_task_version=None):
    """Edit the current task, or create its successor after explicit completion."""
    _positive_id(subject_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValidationError("expected_revision must be a nonnegative integer.")
    if expected_task_version is not None and (type(expected_task_version) is not str
            or len(expected_task_version) != 64 or any(c not in "0123456789abcdef" for c in expected_task_version)):
        raise ValidationError("Некорректная версия следующего действия.")
    values = _next_action_values({"title": title, "due_at": due_at})
    actor_id, organization_id, digest = _pilot_payload(actor=actor, organization=organization,
        operation_key=operation_key, command_type="deal.next_action", subject_id=subject_id,
        expected_revision=expected_revision, next_action=values, expected_task_version=expected_task_version)
    using = router.db_for_write(CrmCommand)
    with transaction.atomic(using=using):
        organization = _locked_organization(using, organization_id)
        previous = _previous_pilot_command(using, organization_id, operation_key)
        pointer = CrmDealNextAction.objects.using(using).select_for_update().filter(
            organization_id=organization_id, deal_id=subject_id).first()
        tasks = _locked_pilot_tasks(using, pointer=pointer, previous=previous)
        actor = _authorize(using, actor_id, organization, "manage_own_next_action")
        if previous:
            return _replay_pilot(using, previous, actor, organization_id, digest, "deal.next_action", tasks)
        deal = _owned_deal(using, actor.pk, organization_id, subject_id)
        if deal.revision != expected_revision:
            raise CommercialConflict("CRM revision changed; reload the card.")
        if deal.is_archived or deal.outcome != "open" or not deal.client_id:
            raise ValidationError("Следующий шаг доступен у открытой сделки с клиентом.")
        task = tasks.get(pointer.task_link.task_id) if pointer else None
        if pointer:
            from pool_service.services.task_feedback import version_for
            _validate_task_link(using, actor, deal, task)
            if expected_task_version is None or version_for(task) != expected_task_version:
                raise CommercialConflict("Следующее действие уже изменено. Обновите карточку; введённый текст сохранён.")
            pointer.full_clean()
        elif expected_task_version is not None:
            raise CommercialConflict("Следующее действие уже изменено. Обновите карточку.")
        terminal = task and (task.is_archived or task.completed_at
            or task.status in {ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED})
        if not task or terminal:
            task, link = _new_next_task(using, actor, deal, values)
            if pointer:
                pointer.task_link = link
                pointer.save(using=using)
            else:
                CrmDealNextAction.objects.using(using).create(
                    organization_id=organization_id, deal=deal, task_link=link)
        else:
            if not task.is_editable or task.status == ServiceTask.STATUS_WAITING:
                raise ValidationError("Измените режим ожидания или ограничения шага в карточке задачи.")
            due = parse_datetime(values["due_at"])
            local = due.astimezone(ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul")))
            if task.title == values["title"] and task.due_at == due:
                raise ValidationError("Следующее действие не изменилось.")
            before_task = {"title": task.title, "due_at": task.due_at.isoformat() if task.due_at else None}
            task.title, task.due_at = values["title"], due
            task.start_date = task.end_date = local.date()
            task.start_time = task.end_time = local.time()
            task.save(using=using, update_fields=["title", "due_at", "start_date", "end_date", "start_time", "end_time", "updated_at"])
            ServiceTaskChange.objects.using(using).create(task=task, changed_by=actor,
                action=ServiceTaskChange.ACTION_UPDATED, field_name="crm_next_action",
                old_value=json.dumps(before_task, ensure_ascii=False), new_value=json.dumps(values, ensure_ascii=False))
        before = deal.revision
        deal.revision += 1
        deal.save(using=using)
        return _record_pilot_command(using, actor=actor, deal=deal, operation_key=operation_key,
            digest=digest, command_type="deal.next_action", task=task, before=before)


def close_commercial_deal_negative(*, actor, organization, operation_key, subject_id,
                                   expected_revision, outcome, reason, expected_task_version):
    """Explicit negative outcome, gated by existing crm_close, with atomic reminder cancellation."""
    from pool_service.services.task_feedback import version_for
    _positive_id(subject_id)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValidationError("Некорректная версия сделки.")
    if type(outcome) is not str or outcome not in {"lost", "cancelled"}:
        raise ValidationError("Выберите отказ или отмену.")
    if type(reason) is not str or not reason.strip() or len(reason.strip()) > 500:
        raise ValidationError("Укажите причину отказа или отмены, до 500 символов.")
    reason = reason.strip()
    if expected_task_version is not None and (type(expected_task_version) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", expected_task_version)):
        raise ValidationError("Некорректная версия следующего действия.")
    actor_id, organization_id, digest = _pilot_payload(actor=actor, organization=organization,
        operation_key=operation_key, command_type="deal.close_negative", subject_id=subject_id,
        expected_revision=expected_revision, outcome=outcome, reason=reason, expected_task_version=expected_task_version)
    using = router.db_for_write(CrmCommand)
    with transaction.atomic(using=using):
        organization = _locked_organization(using, organization_id)
        previous = _previous_pilot_command(using, organization_id, operation_key)
        pointer = CrmDealNextAction.objects.using(using).select_for_update().filter(
            deal_id=subject_id, organization_id=organization_id).first()
        tasks = _locked_pilot_tasks(using, pointer=pointer, previous=previous)
        actor = _authorize(using, actor_id, organization, ("transition_own_deals", "manage_own_next_action"))
        if not organization.crm_close:
            raise PermissionDenied("Закрытие сделок в этой организации пока недоступно.")
        deal = _owned_deal(using, actor_id, organization_id, subject_id)
        task = tasks.get(pointer.task_link.task_id) if pointer else None
        if pointer:
            pointer.full_clean()
            _validate_task_link(using, actor, deal, task)
        if previous:
            if previous.actor_id != actor_id or previous.command_type != "deal.close_negative" or previous.payload_hash != digest:
                raise CommercialConflict("Operation key was used with different content.")
            if previous.result.get("task_id"):
                _validate_task_link(using, actor, deal, tasks.get(previous.result["task_id"]))
            return dict(previous.result)
        if deal.revision != expected_revision:
            raise CommercialConflict("CRM revision changed; reload the card.")
        if deal.is_archived or deal.outcome != "open" or deal.stage.code not in PREPARATORY_STAGES.get(deal.pipeline.code, ()):
            raise ValidationError("Отказ и отмена доступны только на подготовительных этапах открытой сделки.")
        if (task and version_for(task) != expected_task_version) or (not task and expected_task_version is not None):
            raise CommercialConflict("Следующее действие уже изменено. Обновите карточку.")
        terminal = task and (task.is_archived or task.completed_at or task.status in {ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED})
        if task and not terminal:
            if not task.is_editable or task.status == ServiceTask.STATUS_WAITING:
                raise ValidationError("Сначала снимите ожидание или ограничение в карточке задачи.")
            task.status = ServiceTask.STATUS_CANCELLED
            task.is_archived, task.archived_at, task.archived_by = True, timezone.now(), actor
            task.archived_reason = ServiceTask.ARCHIVE_REASON_MANUAL
            task.save(using=using, update_fields=["status", "is_archived", "archived_at", "archived_by", "archived_reason", "updated_at"])
            ServiceTaskChange.objects.using(using).create(task=task, changed_by=actor,
                action=ServiceTaskChange.ACTION_UPDATED, field_name="crm_deal_negative_close")
        before = deal.revision
        deal.outcome, deal.close_reason, deal.closed_at = outcome, reason, timezone.now()
        deal.revision += 1
        deal.save(using=using)
        command = CrmCommand.objects.using(using).create(organization=organization, operation_key=operation_key,
            actor=actor, command_type="deal.close_negative", payload_hash=digest)
        event = CrmEvent.objects.using(using).create(organization=organization, deal=deal, command=command,
            actor=actor, event_type="deal.close_negative", reason=reason,
            safe_diff={"revision": {"before": before, "after": deal.revision},
                "outcome": {"before": "open", "after": outcome},
                "closed_at": {"before": None, "after": deal.closed_at.isoformat()}})
        command.result = {"deal_id": deal.pk, "revision": deal.revision, "event_id": event.pk}
        if task:
            command.result["task_id"] = task.pk
        command.save(using=using, update_fields=["result"])
        return dict(command.result)
