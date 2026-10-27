"""Atomic, authorized lead qualification. No accounting or communication writes."""
from datetime import datetime, timezone as datetime_timezone
from decimal import Decimal, InvalidOperation
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
    CANONICAL_KEY, CrmAccess, CrmCommand, CrmDeal, CrmDealNextAction, CrmEvent, CrmLead,
    CrmLeadDealLink, CrmLeadSource, CrmLeadTaskLink, CrmLeadThread, CrmPipeline, CrmStage,
)
from pool_service.models import Client, Employee, OrganizationAccess, ServiceTask, ServiceTaskChange
from pool_service.services.crm_commercial_commands import (
    PREPARATORY_STAGES, CommercialConflict, _locked_organization,
    _new_next_task, _next_action_values, _positive_id, _validate_task_link,
)

CAPABILITIES = {
    "lead.create": ("create_own_leads",),
    "lead.new_need": ("create_own_leads",),
    "lead.take": ("take_leads",),
    "lead.disposition": ("transition_own_leads",),
    "lead.acknowledge": ("edit_own_leads",),
    "lead.edit": ("edit_own_leads",),
    "lead.transition": ("transition_own_leads",),
    "lead.qualify": ("qualify_own_leads", "create_own_deals", "manage_own_next_action"),
    "lead.add_deal": ("qualify_own_leads", "create_own_deals", "manage_own_next_action"),
}
EDIT_FIELDS = {"title", "need", "client_id", "direction", "specialization", "location", "desired_at",
               "acquisition_source", "first_contact_channel", "budget_kind", "budget_min", "budget_max", "currency"}
ACTIVE_STATES = {"working", "qualifying", "waiting", "deferred"}
CLOSE_RESULTS = {"linked", "non_target", "spam", "duplicate", "no_deal"}


def lead_column(lead):
    if lead.is_archived:
        return "deleted"
    if lead.status == "closed" and lead.result == "spam":
        return "spam"
    if lead.status == "new" or lead.needs_review:
        return "unparsed"
    return "history" if lead.status == "closed" else "working"


def _authorize_lead(using, actor_id, organization, kind):
    actor = get_user_model().objects.using(using).select_for_update().filter(pk=actor_id).first()
    memberships = list(OrganizationAccess.objects.using(using).select_for_update().filter(
        user_id=actor_id, organization=organization).order_by("pk"))
    employees = list(Employee.objects.using(using).select_for_update().filter(
        user_id=actor_id, organization=organization).order_by("pk"))
    grant = CrmAccess.objects.using(using).select_for_update().filter(user_id=actor_id, organization=organization).first()
    caps = ("view_leads",) + CAPABILITIES[kind]
    if (not actor or not actor.is_active or not memberships or not organization.crm_v2_read
            or not organization.crm_v2_write or not grant or not all(getattr(grant, cap) for cap in caps)
            or any(not employee.is_active or employee.employment_status == Employee.STATUS_DISMISSED for employee in employees)):
        raise PermissionDenied("Current lead command access unavailable.")
    if kind in {"lead.qualify", "lead.add_deal"}:
        from pool_service.services.crm_commercial_access import can_view_team_deals
        if not can_view_team_deals(actor, organization):
            raise PermissionDenied("Qualification deal access unavailable.")
    return actor


def _text(value, limit, required=False):
    if type(value) is not str:
        raise ValidationError("CRM text must be a string.")
    value = value.strip()
    if len(value) > limit or (required and not value):
        raise ValidationError("CRM text is empty or too long.")
    return value


def _date(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = parse_datetime(value)
        except ValueError as exc:
            raise ValidationError("Invalid CRM date.") from exc
    if not isinstance(value, datetime) or timezone.is_naive(value):
        raise ValidationError("CRM dates require a timezone.")
    try:
        return value.astimezone(datetime_timezone.utc).isoformat()
    except (OverflowError, ValueError) as exc:
        raise ValidationError("CRM date outside supported range.") from exc


def _normalize(kind, changes):
    if type(kind) is not str or kind not in CAPABILITIES or type(changes) is not dict:
        raise ValidationError("Unknown lead command.")
    allowed = {
        "lead.create": EDIT_FIELDS | {"next_action"},
        "lead.new_need": EDIT_FIELDS | {"next_action"},
        "lead.take": {"next_action"},
        "lead.disposition": {"column", "reason", "next_action", "expected_task_version"},
        "lead.acknowledge": set(),
        "lead.edit": EDIT_FIELDS | {"next_action", "expected_task_version"},
        "lead.transition": {"status", "result", "reason", "follow_up_at", "target_deal_id", "target_lead_id", "next_action", "expected_task_version"},
        "lead.qualify": {"deals", "reason"},
        "lead.add_deal": {"deals", "reason"},
    }[kind]
    if (not changes and kind != "lead.acknowledge") or not set(changes) <= allowed:
        raise ValidationError("Fields unavailable for this lead command.")
    required = {
        "lead.create": {"title", "need", "next_action"}, "lead.new_need": {"title", "need", "next_action"},
        "lead.take": {"next_action"},
        "lead.disposition": {"column", "reason"}, "lead.transition": {"status"},
        "lead.qualify": {"deals"}, "lead.add_deal": {"deals"},
    }.get(kind, set())
    if not required <= set(changes):
        raise ValidationError("Required lead fields are missing.")
    result = {}
    limits = {"title": 255, "need": 10000, "specialization": 40, "location": 255,
              "acquisition_source": 80, "first_contact_channel": 40, "currency": 3, "reason": 500,
              "column": 16, "direction": 24, "status": 16, "budget_kind": 8, "result": 16, "expected_task_version": 64}
    for name, value in changes.items():
        if name in limits:
            result[name] = _text(value, limits[name], required=name in {"title", "status", "result", "expected_task_version"})
        elif name in {"desired_at", "follow_up_at"}:
            result[name] = _date(value)
        elif name.endswith("_id"):
            result[name] = None if name == "client_id" and value is None else _positive_id(value)
        elif name == "next_action":
            result[name] = _next_action_values(value)
        elif name in {"budget_min", "budget_max"}:
            if value is None:
                result[name] = None
            else:
                if type(value) not in {str, int, Decimal}:
                    raise ValidationError("Invalid lead budget.")
                try:
                    number = Decimal(value)
                except InvalidOperation as exc:
                    raise ValidationError("Invalid lead budget.") from exc
                if not number.is_finite() or number < 0 or number > Decimal("9999999999999999.99") or number.as_tuple().exponent < -2:
                    raise ValidationError("Invalid lead budget.")
                result[name] = format(number, ".2f")
        elif name == "deals":
            if type(value) is not list or not 1 <= len(value) <= 10:
                raise ValidationError("Qualification requires one to ten independent deals.")
            rows = []
            for row in value:
                if type(row) is not dict or not {"pipeline_id", "title", "next_action"} <= set(row) or not set(row) <= {"pipeline_id", "title", "next_action", "client_id"}:
                    raise ValidationError("Each deal requires a pipeline, title and next action.")
                normalized = {"pipeline_id": _positive_id(row["pipeline_id"]),
                              "title": _text(row["title"], 255, True), "next_action": _next_action_values(row["next_action"])}
                if "client_id" in row:
                    normalized["client_id"] = _positive_id(row["client_id"])
                rows.append(normalized)
            result[name] = rows
    if result.get("direction", "") not in {"", "construction", "service", "retail", "tender"}:
        raise ValidationError("Unknown lead direction.")
    if "expected_task_version" in result and (len(result["expected_task_version"]) != 64 or any(c not in "0123456789abcdef" for c in result["expected_task_version"])):
        raise ValidationError("Invalid next-action version.")
    return result


def _client(using, organization_id, client_id):
    if client_id is None:
        return None
    from pool_service.client_queries import active_clients
    client = active_clients(Client.objects.using(using).select_for_update()).filter(pk=client_id, organization_id=organization_id).first()
    if client is None:
        raise PermissionDenied("Lead client unavailable in this organization.")
    return client


def _single_source_call_id(using, lead):
    call_ids = list(CrmLeadSource.objects.using(using).filter(
        lead_id__in=[lead.pk, lead.origin_lead_id], organization_id=lead.organization_id,
        inbox__source_type="call").values_list("inbox__source_id", flat=True).distinct()[:2])
    return call_ids[0] if len(call_ids) == 1 else None


def create_lead_next_task(using, actor, lead, values):
    """Create a human-selected follow-up. Automatic intake must not invent an SLA."""
    values = _next_action_values(values)
    due = parse_datetime(values["due_at"])
    local = due.astimezone(ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul")))
    payload = {"crm_lead": {"lead_id": lead.pk}}
    call_id = _single_source_call_id(using, lead)
    if call_id:
        payload["source_call_id"] = call_id
    task = ServiceTask.objects.using(using).create(
        organization_id=lead.organization_id, client_id=lead.client_id,
        created_by=actor, primary_responsible=actor, title=values["title"],
        task_type=ServiceTask.TYPE_CRM_FOLLOWUP, source_type=ServiceTask.SOURCE_MANAGER,
        visibility=ServiceTask.VISIBILITY_PRIVATE, due_at=due,
        start_date=local.date(), end_date=local.date(), start_time=local.time(), end_time=local.time(),
        payload_json=payload,
    )
    task.responsibles.add(actor)
    CrmLeadTaskLink.objects.using(using).create(organization_id=lead.organization_id, lead=lead, task=task, created_by=actor)
    ServiceTaskChange.objects.using(using).create(task=task, changed_by=actor,
        action=ServiceTaskChange.ACTION_CREATED, field_name="crm_lead_next_action")
    lead.next_task = task
    return task


def _terminal(task):
    return not task or task.is_archived or task.completed_at or task.status in {ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED}


def _validate_task(using, lead, task, actor):
    if not task:
        return
    link = CrmLeadTaskLink.objects.using(using).filter(lead_id=lead.pk, task_id=task.pk, organization_id=lead.organization_id).first()
    if not link or task.organization_id != lead.organization_id:
        raise PermissionDenied("Lead next action unavailable.")
    link.full_clean()
    from pool_service.services.task_feedback_permissions import lock_feedback_actor
    lock_feedback_actor(task, actor)


def _set_next(using, actor, lead, task, values, expected_version):
    if _terminal(task):
        return create_lead_next_task(using, actor, lead, values)
    from pool_service.services.task_feedback import version_for
    if expected_version is None or version_for(task) != expected_version:
        raise CommercialConflict("Следующее действие уже изменено. Обновите карточку.")
    if not task.is_editable or task.status == ServiceTask.STATUS_WAITING:
        raise ValidationError("Измените режим ожидания в карточке задачи.")
    due = parse_datetime(values["due_at"])
    local = due.astimezone(ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul")))
    task.title, task.due_at, task.client_id = values["title"], due, lead.client_id
    task.start_date = task.end_date = local.date()
    task.start_time = task.end_time = local.time()
    task.save(update_fields=["title", "due_at", "client", "start_date", "end_date", "start_time", "end_time", "updated_at"])
    ServiceTaskChange.objects.using(using).create(task=task, changed_by=actor,
        action=ServiceTaskChange.ACTION_UPDATED, field_name="crm_lead_next_action")
    return task


def _finish_next(using, actor, lead, task):
    if _terminal(task):
        return
    # Closing qualification supersedes its reminder; it does not claim a call
    # happened or mark unrelated operational/deal tasks as complete.
    task.status = ServiceTask.STATUS_CANCELLED
    task.is_archived = True
    task.archived_at = timezone.now()
    task.archived_by = actor
    task.archived_reason = ServiceTask.ARCHIVE_REASON_MANUAL
    task.save(update_fields=["status", "is_archived", "archived_at", "archived_by", "archived_reason", "updated_at"])
    ServiceTaskChange.objects.using(using).create(task=task, changed_by=actor,
        action=ServiceTaskChange.ACTION_UPDATED, field_name="crm_lead_resolved")


def _write_fields(using, lead, values):
    for field in EDIT_FIELDS & set(values):
        value = values[field]
        if field == "client_id":
            _client(using, lead.organization_id, value)
        elif field == "desired_at":
            value = parse_datetime(value) if value else None
        setattr(lead, field, value)


def _new_lead(using, actor, organization, values, origin=None):
    lead = CrmLead(organization=organization, assignee=actor, status="working", revision=1,
                   acquisition_source="manual", first_contact_channel="manual", last_activity_at=timezone.now())
    lead.origin_lead = origin
    _write_fields(using, lead, values)
    lead.save(using=using)
    create_lead_next_task(using, actor, lead, values["next_action"])
    lead.save(using=using)
    return lead


def _visible(using, actor, organization, lead):
    from pool_service.services.crm_lead_access import can_read_lead
    if not can_read_lead(actor, organization, lead):
        raise PermissionDenied("Lead source unavailable under current access.")
    lead.full_clean()
    if lead.client_id:
        _client(using, organization.pk, lead.client_id)


def _existing_target(using, actor, organization, lead, values):
    result = values.get("result", "")
    if result == "linked":
        if "target_deal_id" not in values or "target_lead_id" in values:
            raise ValidationError("Select the existing deal explicitly.")
        from pool_service.services.crm_commercial_access import deals_for_user
        target = deals_for_user(actor, organization).using(using).select_for_update().filter(pk=values["target_deal_id"]).first()
        if not target or target.is_archived or target.outcome != "open":
            raise PermissionDenied("Existing deal unavailable.")
        target.full_clean()
        lead.linked_deal = target
    elif result == "duplicate":
        if "target_lead_id" not in values or "target_deal_id" in values:
            raise ValidationError("Select the original lead explicitly.")
        target = CrmLead.objects.using(using).select_for_update().filter(pk=values["target_lead_id"], organization=organization).first()
        if not target or target.pk == lead.pk or target.is_archived or target.result == "duplicate":
            raise ValidationError("Original lead unavailable.")
        _visible(using, actor, organization, target)
        lead.duplicate_of = target
    elif {"target_lead_id", "target_deal_id"} & set(values):
        raise ValidationError("Unexpected resolution target.")


def execute_lead_command(*, actor, organization, operation_key, command_type,
                         subject_id=None, expected_revision=None, changes):
    values = _normalize(command_type, changes)
    actor_id = _positive_id(getattr(actor, "pk", None))
    org_id = _positive_id(getattr(organization, "pk", None))
    if type(operation_key) is not str or len(operation_key) > 100:
        raise ValidationError("Invalid operation key.")
    CANONICAL_KEY(operation_key)
    if command_type == "lead.create":
        if subject_id is not None or expected_revision is not None:
            raise ValidationError("A new lead has no subject revision.")
    else:
        _positive_id(subject_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValidationError("Expected lead revision required.")
    payload = {"actor_id": actor_id, "organization_id": org_id, "command_type": command_type,
               "subject_id": subject_id, "expected_revision": expected_revision, "changes": values}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    using = router.db_for_write(CrmCommand)
    with transaction.atomic(using=using):
        organization = _locked_organization(using, org_id)
        previous = CrmCommand.objects.using(using).select_for_update().filter(organization=organization, operation_key=operation_key).first()
        # The organization mutex serializes lead/pointer writers. Lock seed
        # leads with a current read: a plain pre-authorization SELECT would
        # establish an old MySQL RR snapshot for subsequent source checks.
        # Task feedback locks task -> actor; preserve that ordering below.
        task_ids = set(CrmLead.objects.using(using).select_for_update().filter(pk__in=[subject_id, (previous.result.get("lead_id") if previous else None)], organization=organization).values_list("next_task_id", flat=True))
        if previous:
            task_ids.add(previous.result.get("task_id"))
            task_ids.update(CrmDealNextAction.objects.using(using).filter(
                deal_id__in=previous.result.get("deal_ids", []), organization=organization
            ).values_list("task_link__task_id", flat=True))
        tasks = {t.pk: t for t in ServiceTask.objects.using(using).select_for_update().filter(pk__in=task_ids).order_by("pk")}
        actor = _authorize_lead(using, actor_id, organization, command_type)
        lead = None
        if subject_id is not None:
            lead = CrmLead.objects.using(using).select_for_update().filter(pk=subject_id, organization=organization).first()
            if not lead:
                raise PermissionDenied("Lead unavailable.")
            _visible(using, actor, organization, lead)
            if command_type in {"lead.take", "lead.disposition"}:
                if lead.assignee_id not in {None, actor.pk}:
                    raise CommercialConflict("Обращение уже взял другой сотрудник.")
            elif lead.assignee_id != actor.pk:
                raise PermissionDenied("Only own leads may be changed.")
        if previous:
            if previous.actor_id != actor.pk or previous.command_type != command_type or previous.payload_hash != digest:
                raise CommercialConflict("Operation key was used with different content.")
            replay_lead = CrmLead.objects.using(using).select_for_update().get(pk=previous.result["lead_id"], organization=organization)
            _visible(using, actor, organization, replay_lead)
            if replay_lead.assignee_id != actor.pk:
                raise PermissionDenied("Lead ownership changed.")
            if command_type == "lead.transition" and values.get("result") in {"linked", "duplicate"}:
                _existing_target(using, actor, organization, replay_lead, values)
            if replay_lead.next_task_id:
                _validate_task(using, replay_lead, tasks.get(replay_lead.next_task_id), actor)
                if replay_lead.next_task_id not in tasks:
                    raise PermissionDenied("Lead task unavailable.")
            if command_type == "lead.disposition":
                recorded_task = tasks.get(previous.result.get("task_id"))
                if previous.result.get("task_id") and not recorded_task:
                    raise PermissionDenied("Recorded lead task unavailable.")
                if recorded_task and recorded_task.pk != replay_lead.next_task_id:
                    _validate_task(using, replay_lead, recorded_task, actor)
            from pool_service.services.crm_commercial_access import deals_for_user
            ids = previous.result.get("deal_ids", [])
            result_deals = list(deals_for_user(actor, organization).using(using).select_for_update().filter(pk__in=ids).order_by("pk"))
            if len(result_deals) != len(ids):
                raise PermissionDenied("Qualification result unavailable.")
            for deal in result_deals:
                if deal.manager_id != actor.pk:
                    raise PermissionDenied("Qualification ownership changed.")
                deal.full_clean()
                pointer = CrmDealNextAction.objects.using(using).select_for_update().filter(deal=deal, organization=organization).first()
                if not pointer:
                    raise PermissionDenied("Qualification next action unavailable.")
                _validate_task_link(using, actor, deal, tasks.get(pointer.task_link.task_id))
            return dict(previous.result)
        if lead and lead.revision != expected_revision:
            raise CommercialConflict("Обращение уже изменено. Обновите карточку.")
        if lead and (lead.is_archived or lead.status == "closed" and lead.result == "spam") and command_type != "lead.disposition":
            raise ValidationError("Сначала явно восстановите лид из спама или удалённых.")
        before = lead.revision if lead else 0
        before_status = lead.status if lead else None
        before_owner = lead.assignee_id if lead else None
        before_archived = lead.is_archived if lead else False
        before_review = lead.needs_review if lead else False
        before_result = lead.result if lead else ""
        if command_type == "lead.disposition":
            before_column = lead_column(lead)
        task = tasks.get(lead.next_task_id) if lead else None
        if lead:
            _validate_task(using, lead, task, actor)
        additional = {}
        if command_type in {"lead.create", "lead.new_need"}:
            old = lead
            threads = []
            if old:
                if old.status != "closed":
                    raise ValidationError("A new need can be split explicitly from a resolved lead.")
                threads = list(CrmLeadThread.objects.using(using).select_for_update().filter(lead=old, organization=organization).order_by("pk"))
                if not threads:
                    raise ValidationError("This lead has no communication thread to split.")
            lead = _new_lead(using, actor, organization, values, origin=(old.origin_lead or old) if old else None)
            task = lead.next_task
            for thread in threads:
                thread.lead = lead
                thread.save(using=using)
            if old:
                old.needs_review = False
                old.revision += 1
                old.save(using=using)
                additional["previous_lead_id"] = old.pk
            before, before_status = 0, None
        elif command_type == "lead.disposition":
            column = values["column"]
            if column not in {"unparsed", "working", "spam", "deleted", "restore"} or not values["reason"]:
                raise ValidationError("Укажите допустимую колонку и причину перемещения.")
            if lead.assignee_id is None:
                grant = CrmAccess.objects.using(using).select_for_update().get(user=actor, organization=organization)
                if not grant.take_leads:
                    raise PermissionDenied("Free lead claim requires take access.")
                if lead.status != "new" or lead.result:
                    raise ValidationError("Завершённый свободный лид нельзя назначить перемещением.")
                lead.assignee = actor
            spam = lead.status == "closed" and lead.result == "spam"
            if column == "restore":
                if not lead.is_archived:
                    raise ValidationError("Лид уже восстановлен.")
                lead.is_archived = False
                if lead.status != "closed":
                    lead.needs_review = True
            elif column == "deleted":
                if not _terminal(task):
                    from pool_service.services.task_feedback import version_for
                    if values.get("expected_task_version") != version_for(task):
                        raise CommercialConflict("Следующий шаг уже изменён. Обновите карточку.")
                if lead.is_archived:
                    raise ValidationError("Лид уже в удалённых.")
                lead.is_archived = True
                _finish_next(using, actor, lead, task)
            else:
                if lead.is_archived:
                    if spam and column != "spam":
                        raise ValidationError("Удалённый спам сначала возвращается в колонку «Спам».")
                    lead.is_archived = False
                if column == "spam":
                    if spam and before_archived:
                        pass
                    elif lead.status == "closed":
                        raise ValidationError("Завершённый результат не меняется перемещением в спам.")
                    if not _terminal(task):
                        from pool_service.services.task_feedback import version_for
                        if values.get("expected_task_version") != version_for(task):
                            raise CommercialConflict("Следующий шаг уже изменён. Обновите карточку.")
                    lead.status, lead.result, lead.reason = "closed", "spam", values["reason"]
                    lead.follow_up_at = None
                    _finish_next(using, actor, lead, task)
                else:
                    # Only an explicit spam restoration may reopen a final result.
                    # Historical tasks stay cancelled; Work needs a new next step.
                    if spam:
                        lead.status, lead.result, lead.follow_up_at = "new", "", None
                        lead.reason = values["reason"]
                    if column == "working":
                        if lead.status == "closed":
                            raise ValidationError("Завершённая работа не открывается перемещением. Создайте новую потребность явно.")
                        if "next_action" in values:
                            task = _set_next(using, actor, lead, task, values["next_action"], values.get("expected_task_version"))
                        if _terminal(task) or task.due_at is None:
                            raise ValidationError("Для работы укажите реальный следующий шаг и срок.")
                        if lead.status == "new":
                            lead.status = "working"
                        elif lead.status in {"waiting", "deferred"}:
                            lead.follow_up_at = task.due_at
                    else:
                        lead.needs_review = True
            if column != "working" and "next_action" in values:
                raise ValidationError("Следующий шаг задаётся только при перемещении в работу.")
            lead.revision += 1
        elif command_type == "lead.take":
            if lead.assignee_id is not None or lead.status != "new" or lead.result:
                raise CommercialConflict("Обращение уже назначено или обработано.")
            lead.assignee, lead.status = actor, "working"
            task = create_lead_next_task(using, actor, lead, values["next_action"])
            lead.revision += 1
        elif command_type == "lead.acknowledge":
            if lead.status != "closed" or not lead.needs_review:
                raise ValidationError("Only new activity on a resolved lead can be acknowledged.")
            lead.revision += 1
        elif command_type == "lead.edit":
            if lead.status == "closed":
                raise ValidationError("Resolved lead qualification is read-only.")
            if set(values) == {"expected_task_version"}:
                raise ValidationError("No lead changes supplied.")
            _write_fields(using, lead, values)
            if "next_action" in values:
                task = _set_next(using, actor, lead, task, values["next_action"], values.get("expected_task_version"))
            if _terminal(task) or task.due_at is None:
                raise ValidationError("An active lead requires a next action with a date.")
            if task.client_id != lead.client_id:
                task.client_id = lead.client_id
                task.save(update_fields=["client", "updated_at"])
            lead.revision += 1
        elif command_type == "lead.transition":
            if lead.status == "closed":
                raise ValidationError("Resolved leads cannot be reopened implicitly.")
            target = values["status"]
            if target not in ACTIVE_STATES | {"closed"} or target == lead.status:
                raise ValidationError("Lead transition unavailable.")
            reason = values.get("reason", "")
            if target == "closed":
                if values.get("result") not in CLOSE_RESULTS:
                    raise ValidationError("Select an explicit lead result.")
                if values["result"] == "no_deal" and not reason:
                    raise ValidationError("Closing without a deal requires a reason.")
                if {"next_action", "follow_up_at", "expected_task_version"} & set(values):
                    raise ValidationError("Resolved lead cannot schedule a next action.")
                _existing_target(using, actor, organization, lead, values)
                lead.result = values["result"]
                lead.follow_up_at = None
                _finish_next(using, actor, lead, task)
            else:
                if {"result", "target_deal_id", "target_lead_id"} & set(values):
                    raise ValidationError("Active leads cannot have a final result.")
                if target in {"waiting", "deferred"}:
                    if not reason or not values.get("follow_up_at"):
                        raise ValidationError("Waiting requires a reason and check date.")
                    follow = values["follow_up_at"]
                    next_values = values.get("next_action") or {"title": task.title if task else "Проверить ожидание", "due_at": follow}
                    if next_values["due_at"] != follow:
                        raise ValidationError("Next action must match the waiting check date.")
                    task = _set_next(using, actor, lead, task, next_values, values.get("expected_task_version"))
                    lead.follow_up_at = parse_datetime(follow)
                else:
                    if before_status == "qualifying" and target == "working" and not reason:
                        raise ValidationError("A backward transition requires a reason.")
                    if "next_action" in values:
                        task = _set_next(using, actor, lead, task, values["next_action"], values.get("expected_task_version"))
                    lead.follow_up_at = None
                if _terminal(task) or task.due_at is None:
                    raise ValidationError("An active lead requires a next action with a date.")
            lead.status, lead.reason = target, reason
            lead.revision += 1
        else:
            if command_type == "lead.qualify" and (lead.status not in ACTIVE_STATES or lead.result):
                raise ValidationError("Only active leads can be qualified.")
            if command_type == "lead.add_deal" and not (lead.status == "closed" and lead.result == "qualified"):
                raise ValidationError("Additional deals require an already qualified lead.")
            if not lead.need.strip():
                raise ValidationError("Clarify the need before qualification.")
            lead.status, lead.result = "closed", "qualified"
            lead.reason = values.get("reason", "")
            lead.follow_up_at = None
            _finish_next(using, actor, lead, task)
            lead.revision += 1
        if command_type != "lead.disposition" or values["column"] == "working":
            lead.needs_review = False
        lead.save(using=using)
        command = CrmCommand.objects.using(using).create(organization=organization, actor=actor,
            operation_key=operation_key, command_type=command_type, payload_hash=digest)
        if "previous_lead_id" in additional:
            CrmEvent.objects.using(using).create(organization=organization, lead=old, command=command,
                actor=actor, event_type="lead.new_need", safe_diff={
                    "revision": {"before": old.revision - 1, "after": old.revision}})
        if command_type in {"lead.qualify", "lead.add_deal"}:
            ids = []
            for row in values["deals"]:
                client = _client(using, org_id, row.get("client_id", lead.client_id))
                if client is None:
                    raise ValidationError("A qualified deal requires a client or preliminary client.")
                pipeline = CrmPipeline.objects.using(using).select_for_update().filter(pk=row["pipeline_id"], organization=organization, is_active=True).first()
                order = PREPARATORY_STAGES.get(pipeline.code, ()) if pipeline else ()
                stage = CrmStage.objects.using(using).select_for_update().filter(pipeline=pipeline, code=order[0] if order else "", is_active=True).first()
                if not pipeline or not stage:
                    raise ValidationError("Deal pipeline unavailable.")
                deal = CrmDeal.objects.using(using).create(organization=organization, client=client,
                    pipeline=pipeline, stage=stage, manager=actor, title=row["title"], revision=1)
                next_task, link = _new_next_task(using, actor, deal, row["next_action"])
                call_id = _single_source_call_id(using, lead)
                if call_id:
                    next_task.payload_json = {**next_task.payload_json, "source_call_id": call_id}
                    next_task.save(update_fields=["payload_json", "updated_at"])
                CrmDealNextAction.objects.using(using).create(organization=organization, deal=deal, task_link=link)
                CrmLeadDealLink.objects.using(using).create(organization=organization, lead=lead, deal=deal, command=command,
                    kind="qualification" if command_type == "lead.qualify" else "additional")
                CrmEvent.objects.using(using).create(organization=organization, deal=deal, command=command,
                    actor=actor, event_type="deal.create", safe_diff={"revision": {"before": 0, "after": 1}})
                ids.append(deal.pk)
            additional["deal_ids"] = ids
        if command_type == "lead.transition" and lead.result == "linked":
            CrmLeadDealLink.objects.using(using).create(organization=organization, lead=lead, deal=lead.linked_deal,
                command=command, kind="existing")
        diff = {"revision": {"before": before, "after": lead.revision}}
        if before_status is not None:
            diff["status"] = {"before": before_status, "after": lead.status}
        if command_type in {"lead.create", "lead.new_need", "lead.take"}:
            diff["assignee_id"] = {"before": None, "after": actor.pk}
        if command_type == "lead.disposition":
            diff["column"] = {"before": before_column, "after": lead_column(lead)}
            diff["result"] = {"before": before_result, "after": lead.result}
            diff["needs_review"] = {"before": before_review, "after": lead.needs_review}
            diff["is_archived"] = {"before": before_archived, "after": lead.is_archived}
            diff["assignee_id"] = {"before": before_owner, "after": lead.assignee_id}
        if "next_action" in values:
            diff["next_action_changed"] = {"before": False, "after": True}
        if command_type == "lead.edit":
            diff["need_changed"] = {"before": False, "after": True}
        event = CrmEvent.objects.using(using).create(organization=organization, lead=lead, command=command,
            actor=actor, event_type=command_type, safe_diff=diff, reason=values.get("reason", ""))
        result = {"lead_id": lead.pk, "event_id": event.pk, "revision": lead.revision, **additional}
        if lead.next_task_id:
            result["task_id"] = lead.next_task_id
        command.result = result
        command.save(using=using, update_fields=["result"])
        return dict(result)
