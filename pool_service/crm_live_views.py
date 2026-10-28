"""Authenticated, organization-scoped HTTP adapter for the first CRM workspace.

Writes enter the commercial command boundary; this module never saves deals or
tasks directly. Team deal visibility does not imply visibility of linked tasks.
"""
from functools import wraps
from datetime import datetime, time, timedelta
from django.utils.dateparse import parse_date
import json
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Count, F, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .client_queries import active_clients
from .crm_commercial_models import CrmAccess, CrmDeal, CrmEvent, CrmPipeline
from .models import Client, Organization, OrganizationAccess, ServiceTask
from .services.crm_commercial_access import can_view_team_deals
from .services.crm_commercial_commands import (
    CARD_FIELDS, PREPARATORY_STAGES, CommercialConflict, close_commercial_deal_negative, create_commercial_deal,
    execute_commercial_command, set_deal_next_action,
)
from .services.task_feedback import version_for, waiting_control
from .services.task_read_permissions import readable_tasks


PAGE_SIZE = 50
MAX_BODY_BYTES = 16384
EVENT_LABELS = {
    "deal.create": "Сделка создана",
    "deal.edit": "Карточка сделки изменена",
    "deal.close_negative": "Сделка завершена отказом или отменой",
    "deal.transition": "Этап сделки изменён",
    "deal.next_action": "Следующее действие изменено",
}


def _id(value):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 18:
        value = int(value)
    if type(value) is not int or value <= 0 or value > 9223372036854775807:
        raise ValidationError("Некорректный идентификатор.")
    return value


def _organization(request):
    # The explicit organization in every API URL survives filters/drawer changes.
    # No superuser bypass and no cached membership from a different organization.
    if not request.user.is_authenticated or not request.user.is_active:
        raise PermissionDenied
    organizations = Organization.objects.filter(accesses__user_id=request.user.pk).distinct()
    selected = request.GET.getlist("org_id")
    if len(selected) > 1:
        raise PermissionDenied
    if selected:
        try:
            organization = organizations.filter(pk=_id(selected[0])).first()
        except ValidationError as exc:
            raise PermissionDenied from exc
    else:
        organization = organizations.order_by("pk").first()
    if not organization or not can_view_team_deals(request.user, organization):
        raise PermissionDenied
    return organization


def _grant(user, organization):
    return CrmAccess.objects.filter(organization=organization, user_id=user.pk).first()


def _write_capability(grant, organization, name):
    return bool(organization.crm_v2_write and grant and getattr(grant, name, False))


def _deal_queryset(user, organization):
    # Scope every joined relation too: inconsistent historical links must not
    # reveal another organization's client, pipeline, stage, task or events.
    return CrmDeal.objects.filter(
        organization=organization,
        pipeline__organization_id=organization.pk,
        stage__pipeline_id=F("pipeline_id"),
    ).filter(Q(client__isnull=True) | Q(client__organization_id=organization.pk)).select_related(
        "client", "manager", "pipeline", "stage", "next_action__task_link__task",
    )


def _label(user):
    return (user.get_full_name() or user.username) if user else "Не назначен"


def _task_for(deal):
    pointer = getattr(deal, "next_action", None)
    if not pointer:
        return None, False
    link = pointer.task_link
    task = link.task
    if (pointer.organization_id != deal.organization_id or link.organization_id != deal.organization_id
            or link.deal_id != deal.pk or task.organization_id != deal.organization_id
            or task.client_id != deal.client_id or task.crm_item_id is not None):
        return None, True
    return task, False


ATTENTION_CANDIDATE_LIMIT = 1000


def _attention_ids(queryset, user, organization, attention):
    """Classify one explicitly bounded business-visible set before pagination.

    The sentinel check precedes task loading/privacy/timing. Reuse the current
    readable_tasks policy instead of duplicating its source privacy in SQL.
    """
    active = queryset.filter(outcome="open")
    candidate_ids = list(active.order_by("-pk").values_list("pk", flat=True)[:ATTENTION_CANDIDATE_LIMIT + 1])
    if len(candidate_ids) > ATTENTION_CANDIDATE_LIMIT:
        raise ValidationError("Уточните обычные фильтры: для следующего действия доступно не более 1000 открытых сделок.")
    deals = list(active.filter(pk__in=candidate_ids))
    pairs = {deal.pk: _task_for(deal) for deal in deals}
    task_ids = {task.pk for task, invalid in pairs.values() if task and not invalid}
    visible = {task.pk: task for task in readable_tasks(
        ServiceTask.objects.filter(pk__in=task_ids, organization=organization), user)}
    now = timezone.now()
    zone = ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))
    today = now.astimezone(zone).date()
    matched = []
    for deal in deals:
        task, invalid = pairs[deal.pk]
        if invalid or (task and task.pk not in visible):
            continue
        task = visible.get(task.pk) if task else None
        terminal = task and (task.is_archived or task.completed_at
            or task.status in {ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED})
        if attention == "missing":
            if not task or terminal:
                matched.append(deal.pk)
        elif task and not terminal:
            waiting, next_check = waiting_control(task)
            day = task.end_date or task.start_date
            if attention == "today":
                # An internal waiting check is not an agreed appointment. Never
                # fall back to an old date when its check is unknown.
                instant = next_check if waiting else task.due_at
                action_day = (instant.astimezone(zone).date()
                    if instant else (None if waiting else day))
                if action_day == today:
                    matched.append(deal.pk)
            elif not waiting and ((task.due_at and task.due_at < now)
                    or (not task.due_at and day and day < today)):
                matched.append(deal.pk)
    return matched


def _object_maps(deals, user, organization):
    from .services.crm_deal_objects import object_querysets
    pools, owned = object_querysets(user, organization.pk)
    return ({item.pk: item for item in pools.filter(pk__in={deal.pool_id for deal in deals if deal.pool_id})},
            {item.pk: item for item in owned.filter(pk__in={deal.client_object_id for deal in deals if deal.client_object_id})})


def _object_data(deal, pools, owned):
    if deal.object_kind == "planned":
        return {"kind": "planned", "name": deal.planned_object_name, "address": deal.planned_object_address}
    item = pools.get(deal.pool_id) if deal.object_kind == "pool" else owned.get(deal.client_object_id)
    if deal.object_kind == "none":
        return None
    if not item or item.client_id != deal.client_id:
        return {"restricted": True}
    return {"kind": deal.object_kind, "id": item.pk,
        "name": item.get_object_type_display() if deal.object_kind == "pool" else str(item), "address": item.address}


def _serialize_deals(deals, user, organization, grant, *, detail=False):
    deals = list(deals)
    pools, owned = _object_maps(deals, user, organization)
    tasks = {deal.pk: _task_for(deal) for deal in deals}
    task_ids = {task.pk for task, _invalid in tasks.values() if task}
    visible = {task.pk: task for task in readable_tasks(
        ServiceTask.objects.filter(pk__in=task_ids, organization=organization)
        .select_related("parent_task").prefetch_related("responsibles"), user,
    )}
    client_card_access = user.is_superuser or OrganizationAccess.objects.filter(
        organization=organization, user_id=user.pk,
        role__in=("owner", "admin", "manager", "service", "installer"),
    ).exists()
    rows = []
    for deal in deals:
        task, invalid = tasks[deal.pk]
        next_action = None
        if invalid or (task and task.pk not in visible):
            next_action = {"restricted": True}
        elif task:
            task = visible[task.pk]
            waiting, next_check = waiting_control(task)
            next_action = {
                "id": task.pk, "title": task.title,
                "due_at": task.due_at.isoformat() if task.due_at else None,
                "due_date": (task.end_date or task.start_date).isoformat() if not waiting else None,
                "waiting": waiting,
                "next_check_at": next_check.isoformat() if next_check else None,
                "status": task.status, "status_label": task.get_status_display(),
                "completed": bool(task.completed_at) or task.is_archived or task.status in (ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED),
                "url": reverse("task_edit", args=[task.pk]), "can_open": True,
            }
            if detail:
                next_action["version"] = version_for(task)
        object_data = _object_data(deal, pools, owned)
        writable = not (object_data and object_data.get("restricted")) and deal.manager_id == user.pk and deal.outcome == "open" and not deal.is_archived
        rows.append({
            "id": deal.pk, "title": deal.title, "revision": deal.revision,
            "pipeline_id": deal.pipeline_id, "stage_id": deal.stage_id,
            "outcome": deal.outcome, "is_archived": deal.is_archived,
            "work_subject": deal.work_subject, "specialization": deal.specialization,
            "fulfillment_kind": deal.fulfillment_kind, "object": object_data,
            "amount": format(deal.amount, ".2f") if deal.amount is not None else None,
            "currency": deal.currency, "amount_basis": deal.amount_basis,
            "expected_decision_at": deal.expected_decision_at.isoformat() if deal.expected_decision_at else None,
            "close_reason": deal.close_reason, "closed_at": deal.closed_at.isoformat() if deal.closed_at else None,
            "can_close_negative": writable and organization.crm_close
                and _write_capability(grant, organization, "transition_own_deals")
                and _write_capability(grant, organization, "manage_own_next_action")
                and deal.stage.code in PREPARATORY_STAGES.get(deal.pipeline.code, ())
                and not (next_action and (next_action.get("restricted") or next_action.get("waiting"))),
            "client": {"id": deal.client_id, "name": deal.client.name,
                "url": reverse("client_detail", args=[deal.client_id]) if client_card_access else ""} if deal.client_id else None,
            "manager": {"id": deal.manager_id, "name": _label(deal.manager)},
            "can_edit": writable and _write_capability(grant, organization, "edit_own_deals"),
            "can_transition": writable and _write_capability(grant, organization, "transition_own_deals")
                and deal.pipeline.is_active and deal.stage.code in PREPARATORY_STAGES.get(deal.pipeline.code, ()),
            "can_next_action": writable and _write_capability(grant, organization, "manage_own_next_action")
                and not (next_action and (next_action.get("restricted") or next_action.get("waiting"))),
            "next_action": next_action,
        })
    return rows


def _workspace_data(request, organization):
    grant = _grant(request.user, organization)
    pipelines = list(CrmPipeline.objects.filter(organization=organization, is_active=True).prefetch_related("stages"))
    pipeline_id = _id(request.GET["pipeline"]) if request.GET.get("pipeline") else (pipelines[0].pk if pipelines else None)
    if pipeline_id is not None and pipeline_id not in {pipeline.pk for pipeline in pipelines}:
        raise Http404
    owner = request.GET.get("owner", "all")
    if owner not in ("me", "all"):
        raise ValidationError("Выберите корректный фильтр ответственного.")
    query = request.GET.get("q", "").strip()[:200]
    page = _id(request.GET.get("page", "1"))
    if page > 10000:
        raise ValidationError("Уточните фильтры, чтобы перейти к нужным сделкам.")
    outcome = request.GET.get("outcome", "open")
    if outcome not in {"open", "lost", "cancelled", "won", "unknown", "all"}:
        raise ValidationError("Некорректный исход сделки.")
    object_kind = request.GET.get("object", "")
    if object_kind not in {"", "none", "pool", "client_object", "planned"}:
        raise ValidationError("Некорректный тип объекта.")
    stage = _id(request.GET["stage"]) if request.GET.get("stage") else None
    if stage and not any(item.pk == stage for pipeline in pipelines if pipeline.pk == pipeline_id for item in pipeline.stages.all()):
        raise ValidationError("Некорректный этап.")
    specialization = request.GET.get("specialization", "").strip()[:40]
    queryset = _deal_queryset(request.user, organization).filter(pipeline_id=pipeline_id, is_archived=False)
    if outcome != "all":
        queryset = queryset.filter(outcome=outcome)
    if object_kind:
        queryset = queryset.filter(object_kind=object_kind)
    if stage:
        queryset = queryset.filter(stage_id=stage)
    if specialization:
        queryset = queryset.filter(specialization__icontains=specialization)
    for key, lookup in (("decision_from", "gte"), ("decision_to", "lt")):
        raw = request.GET.get(key, "")
        if raw:
            try:
                day = parse_date(raw)
                if not day:
                    raise ValueError
                if key == "decision_to":
                    day += timedelta(days=1)
                bound = datetime.combine(day, time.min, ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul")))
            except (ValueError, OverflowError) as exc:
                raise ValidationError("Некорректная дата решения.") from exc
            queryset = queryset.filter(**{"expected_decision_at__" + lookup: bound})
    if owner == "me":
        queryset = queryset.filter(manager_id=request.user.pk)
    if query:
        from .services.crm_deal_objects import object_querysets
        pools, owned = object_querysets(request.user, organization.pk)
        queryset = queryset.filter(Q(title__icontains=query) | Q(client__name__icontains=query)
            | Q(work_subject__icontains=query) | Q(specialization__icontains=query)
            | Q(object_kind="planned", planned_object_name__icontains=query)
            | Q(object_kind="planned", planned_object_address__icontains=query)
            | (Q(pool_id__in=pools.filter(address__icontains=query).values("pk")) & Q(pool__client_id=F("client_id")))
            | (Q(client_object_id__in=owned.filter(Q(name__icontains=query) | Q(address__icontains=query)).values("pk")) & Q(client_object__client_id=F("client_id"))))
    attention = request.GET.get("attention", "all")
    if attention not in {"all", "today", "overdue", "missing"}:
        raise ValidationError("Некорректный фильтр следующего действия.")
    attention_total = None
    if attention != "all":
        matched_ids = _attention_ids(queryset, request.user, organization, attention)
        attention_total = len(matched_ids)
        queryset = queryset.filter(pk__in=matched_ids)
    stage_totals = [{"stage_id": row["stage_id"], "currency": row["currency"],
        "amount": format(row["amount_total"], ".2f") if row["amount_total"] is not None else None,
        "count": row["count"], "unknown_count": row["count"] - row["known_count"]}
        for row in queryset.order_by().values("stage_id", "currency").annotate(
            amount_total=Sum("amount"), count=Count("pk"), known_count=Count("amount"))]
    offset = (page - 1) * PAGE_SIZE
    deals = list(queryset.order_by("-pk")[offset:offset + PAGE_SIZE + 1])
    return {
        "organization_id": organization.pk, "organization_name": organization.name, "actor_id": request.user.pk,
        "capabilities": {"create": _write_capability(grant, organization, "create_own_deals")
            and _write_capability(grant, organization, "manage_own_next_action") and bool(pipelines)},
        "pipelines": [{"id": pipeline.pk, "code": pipeline.code, "name": pipeline.name,
            "stages": [{"id": stage.pk, "code": stage.code, "name": stage.name, "position": stage.position,
                "can_transition": stage.is_active and stage.code in PREPARATORY_STAGES.get(pipeline.code, ())}
                for stage in pipeline.stages.all()]} for pipeline in pipelines],
        "deals": _serialize_deals(deals[:PAGE_SIZE], request.user, organization, grant),
        "selected_pipeline_id": pipeline_id, "has_more": len(deals) > PAGE_SIZE,
        "page": page, "has_previous": page > 1,
        "page_size": PAGE_SIZE, "q": query, "owner": owner, "stage_totals": stage_totals,
        "attention": attention, "attention_total": attention_total,
        "outcome": outcome, "object": object_kind, "stage": stage, "specialization": specialization,
        "decision_from": request.GET.get("decision_from", ""), "decision_to": request.GET.get("decision_to", ""),
    }


def _api(view):
    @wraps(view)
    @never_cache
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Войдите в сервис, затем повторите действие.", "code": "auth_required"}, status=401)
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied:
            return JsonResponse({"error": "Доступ к этому действию сейчас отсутствует.", "code": "forbidden"}, status=403)
        except Http404:
            return JsonResponse({"error": "Карточка или воронка недоступна.", "code": "not_found"}, status=404)
        except CommercialConflict:
            return JsonResponse({"error": "Карточка или задача уже изменена. Обновите её и проверьте данные перед сохранением.", "code": "conflict"}, status=409)
        except ValidationError as exc:
            # Model constraint names and internal identifiers are not useful UI.
            messages = exc.messages
            message = next((text for text in messages if any("а" <= char.lower() <= "я" for char in text)),
                "Не удалось сохранить: проверьте поля, дату следующего действия и допустимый этап.")
            return JsonResponse({"error": message[:500], "code": "invalid"}, status=400)
    return wrapped


@login_required
@never_cache
@ensure_csrf_cookie
@require_GET
def workspace(request):
    # HTML entry can offer explicit owner setup; API authorization stays strict.
    from .crm_pilot_views import choose_entry, entry_for, explicit_organization
    organization = explicit_organization(request)
    if organization is None:
        return choose_entry(request)
    entry = entry_for(request, organization)
    if not entry:
        raise PermissionDenied
    if entry.startswith(reverse("crm_live_start") + "?"):
        return redirect(entry)
    from .views import _crm_redirect_if_access_blocked
    blocked = _crm_redirect_if_access_blocked(request, organization)
    if blocked:
        return blocked
    try:
        data = _workspace_data(request, organization)
    except ValidationError as exc:
        # A saved invalid/too-wide filter must still open a usable filter shell.
        # Access errors and unavailable pipeline/card 404s remain unchanged.
        original = request.GET
        try:
            request.GET = original.copy()
            request.GET["attention"], request.GET["page"] = "all", "1"
            try:
                data = _workspace_data(request, organization)
            except ValidationError:
                from django.http import QueryDict
                request.GET = QueryDict(mutable=True)
                request.GET["org_id"] = str(organization.pk)
                data = _workspace_data(request, organization)
        finally:
            request.GET = original
        message = exc.messages[0] if exc.messages else "Проверьте условия поиска."
        data["initial_notice"] = "Сохранённый фильтр не применён. " + message[:500]
    try:
        zone = ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Barnaul")
    offset = timezone.now().astimezone(zone).strftime("%z")
    scope = "?" + urlencode({"org_id": organization.pk})
    data.update({
        "timezone": str(zone), "utc_offset": offset[:3] + ":" + offset[3:],
        "urls": {"data": reverse("crm_live_data") + scope,
            "clients": reverse("crm_live_clients") + scope,
            "objects": reverse("crm_live_objects") + scope,
            "command": reverse("crm_live_command") + scope,
            "detail_prefix": reverse("crm_live_detail", args=[1]).rsplit("1/", 1)[0]},
    })
    return render(request, "pool_service/crm_live/workspace.html", {
        "page_title": "", "page_subtitle": "", "active_tab": "crm",
        "crm_bootstrap": data, "crm_workspace_url": reverse("crm_live_workspace") + scope,
    })


@_api
@require_GET
def data(request):
    organization = _organization(request)
    return JsonResponse(_workspace_data(request, organization))


@_api
@require_GET
def clients(request):
    organization = _organization(request)
    if not _write_capability(_grant(request.user, organization), organization, "create_own_deals"):
        raise PermissionDenied
    query = request.GET.get("q", "").strip()[:200]
    if len(query) < 3:
        return JsonResponse({"clients": []})
    rows = active_clients(Client.objects.filter(organization=organization)).filter(
        Q(name__icontains=query) | Q(phone__icontains=query),
    ).order_by("name", "pk")[:20]
    return JsonResponse({"clients": [{"id": client.pk, "name": client.name} for client in rows]})


@_api
@require_GET
def objects(request):
    organization = _organization(request)
    grant = _grant(request.user, organization)
    client_id = _id(request.GET.get("client_id"))
    if request.GET.get("deal_id"):
        deal = get_object_or_404(_deal_queryset(request.user, organization), pk=_id(request.GET["deal_id"]),
            manager_id=request.user.pk, client_id=client_id, outcome="open", is_archived=False)
        if not _write_capability(grant, organization, "edit_own_deals"):
            raise PermissionDenied
    elif not _write_capability(grant, organization, "create_own_deals"):
        raise PermissionDenied
    from .client_crm_views import _can_view_client_card
    client = get_object_or_404(active_clients(Client.objects.filter(organization=organization)), pk=client_id)
    if not _can_view_client_card(request.user, client):
        raise PermissionDenied
    from .services.crm_deal_objects import object_querysets
    pools, owned = object_querysets(request.user, organization.pk)
    query = request.GET.get("q", "").strip()[:200]
    pools = pools.filter(client_id=client_id)
    owned = owned.filter(client_id=client_id)
    if query:
        pools = pools.filter(address__icontains=query)
        owned = owned.filter(Q(name__icontains=query) | Q(address__icontains=query))
    rows = [{"kind": "pool", "id": item.pk, "name": item.get_object_type_display(), "address": item.address}
        for item in pools.order_by("pk")[:20]]
    rows += [{"kind": "client_object", "id": item.pk, "name": str(item), "address": item.address}
        for item in owned.order_by("pk")[:20]]
    return JsonResponse({"objects": rows, "bounded": True})


@_api
@require_GET
def detail(request, deal_id):
    organization = _organization(request)
    deal = get_object_or_404(_deal_queryset(request.user, organization), pk=deal_id)
    row = _serialize_deals([deal], request.user, organization, _grant(request.user, organization), detail=True)[0]
    from .services.crm_need_deals import deal_need_provenance
    row['need_provenance'] = deal_need_provenance(deal, request.user, organization)
    events = CrmEvent.objects.filter(organization=organization, deal=deal).select_related("actor").order_by("-pk")[:30]
    return JsonResponse({"deal": row, "events": [{
        "type": event.event_type, "label": EVENT_LABELS.get(event.event_type, "Изменение сделки"),
        "at": event.occurred_at.isoformat(), "actor": _label(event.actor),
        # Task content remains in its protected source. Only stage-change
        # reasons belong on the team-visible deal timeline in this release.
        "reason": event.reason if event.event_type in {"deal.transition", "deal.close_negative"} else "",
    } for event in events]})


@_api
@require_POST
def command(request):
    organization = _organization(request)
    from .services.permissions import company_has_access
    if not company_has_access(organization):
        raise PermissionDenied
    if request.content_type != "application/json":
        raise ValidationError("Ожидается запрос JSON.")
    try:
        if int(request.META.get("CONTENT_LENGTH") or 0) > MAX_BODY_BYTES or len(request.body) > MAX_BODY_BYTES:
            raise ValidationError("Слишком большой запрос.")
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError("Некорректный запрос JSON.") from exc
    allowed = {"operation_key", "command_type", "subject_id", "expected_revision", "changes"}
    if type(payload) is not dict or not payload.keys() <= allowed or not {"operation_key", "command_type", "changes"} <= payload.keys():
        raise ValidationError("Некорректный состав запроса.")
    kind, changes = payload["command_type"], payload["changes"]
    if type(kind) is not str or type(changes) is not dict:
        raise ValidationError("Некорректная команда.")
    common = {"actor": request.user, "organization": organization, "operation_key": payload["operation_key"]}
    if kind == "deal.create":
        if not {"title", "client_id", "pipeline_id", "next_action"} <= changes.keys() or not changes.keys() <= {"title", "client_id", "pipeline_id", "next_action", "card_fields"} or payload.get("subject_id") is not None or payload.get("expected_revision") is not None:
            raise ValidationError("Укажите клиента, название, воронку и следующее действие с датой.")
        result = create_commercial_deal(**common, **changes)
    elif kind == "deal.next_action":
        if not {"title", "due_at"} <= changes.keys() or not changes.keys() <= {"title", "due_at", "expected_task_version"}:
            raise ValidationError("Укажите следующее действие и дату.")
        result = set_deal_next_action(**common, subject_id=payload.get("subject_id"),
            expected_revision=payload.get("expected_revision"), **changes)
    elif kind == "deal.close_negative":
        if set(changes) != {"outcome", "reason", "expected_task_version"}:
            raise ValidationError("Укажите исход, причину и текущую версию следующего шага.")
        result = close_commercial_deal_negative(**common, subject_id=payload.get("subject_id"),
            expected_revision=payload.get("expected_revision"), **changes)
    elif kind in ("deal.edit", "deal.transition"):
        result = execute_commercial_command(**common, command_type=kind, subject_id=payload.get("subject_id"),
            expected_revision=payload.get("expected_revision"), changes=changes)
    else:
        raise ValidationError("Эта команда недоступна в рабочей CRM.")
    # Recheck current read access after commit. A lost/denied read must not be
    # treated by the client as permission to send a new operation key.
    try:
        organization = _organization(request)
        deal = get_object_or_404(_deal_queryset(request.user, organization), pk=result["deal_id"])
        row = _serialize_deals([deal], request.user, organization, _grant(request.user, organization), detail=True)[0]
    except (PermissionDenied, Http404):
        return JsonResponse({"error": "Результат сохранения сейчас недоступен. Повторите проверку той же операции.",
            "code": "verify_required"}, status=503)
    return JsonResponse({"result": result, "deal": row})
