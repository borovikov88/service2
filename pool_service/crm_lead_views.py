"""Current, scoped HTTP adapter for lead intake and qualification.

All writes enter the lead command service. Source content is never serialized by
this adapter; the source access service supplies separately authorized locators.
"""
from functools import wraps
import json
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_POST

from .client_queries import active_clients
from .crm_commercial_models import CrmAccess, CrmEvent, CrmIntakeActivation, CrmLead, CrmLeadTaskLink, CrmPipeline
from .crm_live_views import _id, CommercialConflict
from .models import Client, Organization, ServiceTask
from .services.task_read_permissions import readable_tasks
from .services.task_feedback import version_for, waiting_control

PAGE_SIZE = 50
MAX_BODY_BYTES = 65536
STATES = dict(CrmLead._meta.get_field("status").choices)
RESULTS = dict(CrmLead._meta.get_field("result").choices)
EVENTS = {"lead.create": "Лид создан", "lead.take": "Обращение взято в работу",
    "lead.edit": "Карточка изменена", "lead.transition": "Состояние изменено",
    "lead.qualify": "Квалификация завершена", "lead.add_deal": "Добавлена сделка",
    "lead.new_need": "Открыта новая потребность", "lead.incoming": "Поступило обращение",
    "lead.acknowledge": "Новое обращение разобрано как продолжение текущей работы"}
INTAKE_REASONS = {"unassigned": "Требуется ответственный", "missed_call": "Пропущенный звонок",
    "employee_unmapped": "Принявший сотрудник не сопоставлен", "employee_ambiguous": "Неоднозначное сопоставление сотрудника",
    "employee_unavailable": "Принявший сотрудник недоступен", "needs_qualification": "Требуется квалификация"}
ROUTING_REASONS = {"unassigned", "employee_unmapped", "employee_ambiguous", "employee_unavailable"}


def _organization(request):
    from .services.crm_lead_access import can_view_leads
    values = request.GET.getlist("org_id")
    if len(values) != 1:
        raise PermissionDenied
    try:
        organization_id = _id(values[0])
    except ValidationError as exc:
        raise PermissionDenied from exc
    organization = Organization.objects.filter(pk=organization_id).first()
    if not organization or not can_view_leads(request.user, organization):
        raise PermissionDenied
    return organization


def _capabilities(user, organization):
    grant = CrmAccess.objects.filter(user_id=user.pk, organization=organization).first()
    from .services.permissions import company_has_access
    enabled = organization.crm_v2_write and company_has_access(organization)
    names = ("create_own_leads", "take_leads", "edit_own_leads", "transition_own_leads", "qualify_own_leads",
        "create_own_deals", "manage_own_next_action")
    return {name: bool(enabled and grant and getattr(grant, name, False)) for name in names}


def _queryset(user, organization):
    from .services.crm_lead_access import leads_for_user
    return leads_for_user(user, organization).select_related("client", "assignee", "next_task")


def _serialize(lead, user, capabilities, *, detail=False, visible_tasks=None):
    own = lead.assignee_id == user.pk
    opened = not lead.is_archived and lead.status != "closed"
    row = {"id": lead.pk, "title": lead.title, "revision": lead.revision,
        "status": lead.status, "status_label": STATES.get(lead.status, lead.status),
        "result": lead.result, "result_label": RESULTS.get(lead.result, ""),
        "client": {"id": lead.client_id, "name": lead.client.name} if lead.client_id else None,
        "assignee": {"id": lead.assignee_id,
            "name": (lead.assignee.get_full_name() or lead.assignee.username)} if lead.assignee_id else None,
        "received_at": lead.received_at.isoformat(),
        "follow_up_at": lead.follow_up_at.isoformat() if lead.follow_up_at else None,
        "can_take": opened and lead.status == "new" and lead.assignee_id is None and capabilities["take_leads"],
        "can_edit": own and opened and capabilities["edit_own_leads"],
        "can_transition": own and opened and capabilities["transition_own_leads"],
        "can_qualify": own and opened and all(capabilities[name] for name in ("qualify_own_leads", "create_own_deals", "manage_own_next_action")),
        "can_add_deal": own and lead.result == "qualified" and all(capabilities[name] for name in ("qualify_own_leads", "create_own_deals", "manage_own_next_action")),
        "can_new_need": detail and own and lead.status == "closed" and capabilities["create_own_leads"] and lead.threads.exists(),
        "can_acknowledge": own and lead.status == "closed" and lead.needs_review and capabilities["edit_own_leads"],
        "needs_review": lead.needs_review, "next_action": None,
        "intake_reason_label": "" if lead.assignee_id and lead.intake_reason in ROUTING_REASONS else INTAKE_REASONS.get(lead.intake_reason, ""),
        "last_activity_at": lead.last_activity_at.isoformat() if lead.last_activity_at else None}
    if lead.next_task_id:
        task = (visible_tasks or {}).get(lead.next_task_id)
        if not task or task.organization_id != lead.organization_id or task.client_id != lead.client_id or task.crm_item_id:
            row["next_action"] = {"restricted": True}
        else:
            row["next_action"] = {"id": task.pk, "title": task.title,
                "due_at": task.due_at.isoformat() if task.due_at else None,
                "completed": task.is_archived or task.status in (ServiceTask.STATUS_DONE, ServiceTask.STATUS_CANCELLED),
                "url": reverse("task_edit", args=[task.pk]),
                "waiting": waiting_control(task)[0] or task.status == ServiceTask.STATUS_WAITING}
            if detail:
                row["next_action"]["version"] = version_for(task)
    if detail:
        row.update(need=lead.need, reason=lead.reason,
            acquisition_source=lead.acquisition_source, first_contact_channel=lead.first_contact_channel,
            budget_kind=lead.budget_kind,
            budget_min=str(lead.budget_min) if lead.budget_min is not None else "",
            budget_max=str(lead.budget_max) if lead.budget_max is not None else "", currency=lead.currency)
        for field in ("direction", "specialization", "location"):
            row[field] = getattr(lead, field, "")
        desired = getattr(lead, "desired_at", None)
        row["desired_at"] = desired.isoformat() if desired else None
    return row


def _serialize_many(leads, user, capabilities, *, detail=False):
    leads = list(leads)
    ids = [lead.next_task_id for lead in leads if lead.next_task_id]
    visible = {task.pk: task for task in readable_tasks(ServiceTask.objects.filter(pk__in=ids).select_related("parent_task").prefetch_related("responsibles"), user)}
    # A pointer alone cannot turn an unrelated task into a commercial next step.
    links = set(CrmLeadTaskLink.objects.filter(lead_id__in=[lead.pk for lead in leads], task_id__in=ids,
        organization_id=leads[0].organization_id if leads else None).values_list("lead_id", "task_id"))
    for lead in leads:
        if lead.next_task_id and (lead.pk, lead.next_task_id) not in links:
            visible.pop(lead.next_task_id, None)
    return [_serialize(lead, user, capabilities, detail=detail, visible_tasks=visible) for lead in leads]


def _data(request, organization):
    capabilities = _capabilities(request.user, organization)
    mode = request.GET.get("mode", "unassigned")
    status = request.GET.get("status", "")
    if mode not in {"unassigned", "mine", "all", "closed", "review"} or status and status not in STATES:
        raise ValidationError("Выберите корректный фильтр лидов.")
    page_value = request.GET.get("page", "1")
    if not page_value.isascii() or not page_value.isdigit() or len(page_value) > 8 or int(page_value) < 1:
        raise ValidationError("Некорректная страница.")
    query = request.GET.get("q", "").strip()[:200]
    queryset = _queryset(request.user, organization).filter(is_archived=False)
    if mode == "review":
        queryset = queryset.filter(needs_review=True)
    elif mode == "closed":
        queryset = queryset.filter(status="closed")
    else:
        queryset = queryset.exclude(status="closed")
    if mode == "unassigned":
        queryset = queryset.filter(assignee__isnull=True)
    elif mode == "mine":
        queryset = queryset.filter(assignee_id=request.user.pk)
    if status:
        queryset = queryset.filter(status=status)
    if query:
        queryset = queryset.filter(Q(title__icontains=query) | Q(need__icontains=query) | Q(client__name__icontains=query))
    paginator = Paginator(queryset.order_by("-received_at", "-pk"), PAGE_SIZE)
    page = paginator.get_page(int(page_value))
    activation = CrmIntakeActivation.objects.filter(organization=organization).first()
    return {"leads": _serialize_many(page, request.user, capabilities),
        "page": page.number, "pages": paginator.num_pages, "total": paginator.count,
        "page_size": PAGE_SIZE, "q": query, "mode": mode, "status": status,
        "can_create": capabilities["create_own_leads"],
        "intake": {"enabled": organization.crm_intake_process,
            "last_run_at": activation.last_run_at.isoformat() if activation and activation.last_run_at else None,
            "needs_attention": bool(activation and activation.last_error)}}


def _api(view):
    @wraps(view)
    @never_cache
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "Войдите в сервис и повторите действие.", "code": "auth_required"}, status=401)
        try:
            return view(request, *args, **kwargs)
        except PermissionDenied:
            return JsonResponse({"error": "Доступ к этому действию сейчас отсутствует.", "code": "forbidden"}, status=403)
        except Http404:
            return JsonResponse({"error": "Карточка недоступна.", "code": "not_found"}, status=404)
        except CommercialConflict:
            return JsonResponse({"error": "Карточка уже изменена. Обновите её и проверьте введённые данные.", "code": "conflict"}, status=409)
        except ValidationError as exc:
            message = next((value for value in exc.messages if any("а" <= char.lower() <= "я" for char in value)),
                "Проверьте поля, дату следующего шага и допустимость действия.")
            return JsonResponse({"error": message[:500], "code": "invalid"}, status=400)
    return wrapped


@login_required
@never_cache
@ensure_csrf_cookie
@require_GET
def workspace(request):
    organization = _organization(request)
    from .views import _crm_redirect_if_access_blocked
    blocked = _crm_redirect_if_access_blocked(request, organization)
    if blocked:
        return blocked
    try:
        data = _data(request, organization)
    except ValidationError:
        # Bad URL state must not silently open a different organization or list.
        return JsonResponse({"error": "Некорректные фильтры лидов."}, status=400)
    try:
        zone = ZoneInfo(getattr(settings, "COMMUNICATION_TIME_ZONE", "Asia/Barnaul"))
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("Asia/Barnaul")
    offset = timezone.now().astimezone(zone).strftime("%z")
    scope = "?" + urlencode({"org_id": organization.pk})
    data.update(organization_id=organization.pk, organization_name=organization.name,
        timezone=str(zone), utc_offset=offset[:3] + ":" + offset[3:],
        states=STATES, results=RESULTS,
        pipelines=list(CrmPipeline.objects.filter(organization=organization, is_active=True).values("id", "code", "name")),
        urls={"data": reverse("crm_live_lead_data") + scope,
            "command": reverse("crm_live_lead_command") + scope,
            "clients": reverse("crm_live_lead_clients") + scope,
            "targets": reverse("crm_live_lead_targets") + scope,
            "detail_prefix": reverse("crm_live_lead_detail", args=[1]).rsplit("1/", 1)[0],
            "deals": reverse("crm_live_workspace") + scope})
    return render(request, "pool_service/crm_live/leads.html", {
        "page_title": "", "page_subtitle": "", "active_tab": "crm", "lead_bootstrap": data})


@_api
@require_GET
def data(request):
    return JsonResponse(_data(request, _organization(request)))


def _detail(request, organization, lead_id):
    from .services.crm_lead_access import can_read_lead, source_metadata
    lead = get_object_or_404(_queryset(request.user, organization), pk=lead_id)
    if not can_read_lead(request.user, organization, lead):
        raise Http404
    events = CrmEvent.objects.filter(organization=organization, lead=lead).select_related("actor").order_by("-pk")[:30]
    from .services.crm_commercial_access import deals_for_user
    deals = deals_for_user(request.user, organization).filter(lead_links__lead=lead, lead_links__organization=organization).distinct().order_by("pk")[:50]
    duplicate = None
    if lead.duplicate_of_id:
        target = _queryset(request.user, organization).filter(pk=lead.duplicate_of_id).first()
        if target and can_read_lead(request.user, organization, target):
            duplicate = {"id": target.pk, "title": target.title}
    return {"lead": _serialize_many([lead], request.user, _capabilities(request.user, organization), detail=True)[0],
        "events": [{"label": EVENTS.get(event.event_type, "Изменение карточки"),
            "at": event.occurred_at.isoformat(), "reason": event.reason,
            "actor": (event.actor.get_full_name() or event.actor.username) if event.actor else "Система"} for event in events],
        "sources": source_metadata(user=request.user, organization=organization, lead=lead), "duplicate_of": duplicate,
        "deals": [{"id": deal.pk, "title": deal.title, "pipeline_id": deal.pipeline_id,
            "url": reverse("crm_live_workspace") + "?" + urlencode({"org_id": organization.pk, "pipeline": deal.pipeline_id, "deal": deal.pk})} for deal in deals]}


@_api
@require_GET
def detail(request, lead_id):
    return JsonResponse(_detail(request, _organization(request), lead_id))


@_api
@require_GET
def clients(request):
    organization = _organization(request)
    capabilities = _capabilities(request.user, organization)
    if not any(capabilities[name] for name in ("create_own_leads", "edit_own_leads", "qualify_own_leads")):
        raise PermissionDenied
    query = request.GET.get("q", "").strip()[:200]
    if len(query) < 3:
        return JsonResponse({"clients": []})
    queryset = active_clients(Client.objects.filter(organization=organization)).filter(Q(name__icontains=query) | Q(phone__icontains=query))
    return JsonResponse({"clients": list(queryset.order_by("name", "pk").values("id", "name")[:20])})


@_api
@require_GET
def targets(request):
    organization = _organization(request)
    if not _capabilities(request.user, organization)["transition_own_leads"]:
        raise PermissionDenied
    query = request.GET.get("q", "").strip()[:200]
    kind = request.GET.get("kind", "lead")
    if kind not in {"lead", "deal"}:
        raise ValidationError("Некорректный тип связи.")
    if len(query) < 3:
        return JsonResponse({"targets": []})
    if kind == "lead":
        queryset = _queryset(request.user, organization)
    else:
        from .services.crm_commercial_access import deals_for_user
        queryset = deals_for_user(request.user, organization)
    return JsonResponse({"targets": list(queryset.filter(title__icontains=query).order_by("-pk").values("id", "title")[:20])})


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
    if type(payload["command_type"]) is not str or type(payload["changes"]) is not dict or payload["command_type"] not in {"lead.create", "lead.take", "lead.edit", "lead.transition", "lead.qualify", "lead.add_deal", "lead.new_need", "lead.acknowledge"}:
        raise ValidationError("Эта команда недоступна.")
    from .services.crm_lead_commands import execute_lead_command
    result = execute_lead_command(actor=request.user, organization=organization, **payload)
    # A committed write followed by loss of read rights is uncertain to the
    # browser. It must keep the original immutable key/payload for verification.
    try:
        organization = _organization(request)
        response = _detail(request, organization, result["lead_id"])
    except (PermissionDenied, Http404):
        return JsonResponse({"error": "Результат сохранения сейчас недоступен. Проверьте ту же операцию.", "code": "verify_required"}, status=503)
    response["result"] = result
    return JsonResponse(response)
