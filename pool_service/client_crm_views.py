import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.models import User
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .client_crm_import import (
    request_client_apply,
    request_client_import_scan,
    resolve_import_candidate,
)
from .client_crm_models import ClientCRMProfile, ClientImportCandidate, ClientImportRun
from .communication_models import CommunicationAccess
from .client_queries import active_clients
from .client_merge import merge_clients, merge_suggestions
from .models import Client, CrmItem, OrganizationAccess, Pool, ServiceTask


IMPORT_ROLES = {"owner", "admin"}
CLIENT_VIEW_ROLES = {"owner", "admin", "service", "installer", "manager"}
logger = logging.getLogger(__name__)


def _can_manage_import(user, organization_id):
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    return OrganizationAccess.objects.filter(
        user=user,
        organization_id=organization_id,
        role__in=IMPORT_ROLES,
    ).exists()


def _can_view_client(user, client):
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return True
    if not client.organization_id:
        return False
    return OrganizationAccess.objects.filter(
        user=user,
        organization_id=client.organization_id,
        role__in=CLIENT_VIEW_ROLES,
    ).exists()


def _can_manage_client_profile(user, client):
    if not user.is_authenticated or not user.is_active:
        return False
    if user.is_superuser:
        return False
    if not client.organization_id:
        return False
    return OrganizationAccess.objects.filter(
        user=user,
        organization_id=client.organization_id,
        role__in=IMPORT_ROLES,
    ).exists()


def _user_label(user):
    if not user:
        return ""
    return user.get_full_name() or user.username


@login_required
def client_detail(request, client_id):
    client = get_object_or_404(
        active_clients(
            Client.objects.select_related("organization", "crm_profile")
        ),
        pk=client_id,
    )
    if not _can_view_client(request.user, client):
        return HttpResponseForbidden()

    profile = getattr(client, "crm_profile", None)
    can_manage_profile = _can_manage_client_profile(request.user, client)

    if request.method == "POST":
        if not can_manage_profile:
            return HttpResponseForbidden()
        profile, _ = ClientCRMProfile.objects.get_or_create(client=client)

        def resolve_user(raw_value):
            raw_value = (raw_value or "").strip()
            if not raw_value:
                return None
            try:
                user_id = int(raw_value)
            except (TypeError, ValueError):
                raise ValueError("Некорректный сотрудник.")
            user = (
                User.objects.filter(
                    pk=user_id,
                    is_active=True,
                    organizationaccess__organization_id=client.organization_id,
                )
                .distinct()
                .first()
            )
            if not user:
                raise ValueError("Сотрудник не найден в организации.")
            return user

        try:
            profile.manager = resolve_user(request.POST.get("manager"))
            profile.responsible = resolve_user(request.POST.get("responsible"))
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            profile.notes = (request.POST.get("notes") or "").strip()
            profile.save(
                update_fields=[
                    "manager",
                    "responsible",
                    "notes",
                    "updated_at",
                ]
            )
            messages.success(request, "Карточка клиента обновлена.")
        return redirect("client_detail", client_id=client.id)

    contacts = list(client.crm_contacts.order_by("kind", "-is_primary", "id"))
    pools = list(
        Pool.objects.filter(client=client, is_deleted=False)
        .order_by("address", "id")
    )

    tasks_qs = (
        ServiceTask.objects.filter(
            Q(client=client) | Q(pool__client=client),
            organization_id=client.organization_id,
        )
        .exclude(
            is_archived=True,
            archived_reason=ServiceTask.ARCHIVE_REASON_DELETED,
        )
        .select_related("pool", "primary_responsible", "created_by")
        .prefetch_related("responsibles")
        .distinct()
        .order_by("-updated_at", "-id")
    )
    user_roles = set()
    if client.organization_id and not request.user.is_superuser:
        user_roles = set(
            OrganizationAccess.objects.filter(
                user=request.user,
                organization_id=client.organization_id,
            ).values_list("role", flat=True)
        )
    is_admin = request.user.is_superuser or bool(user_roles & IMPORT_ROLES)
    if not is_admin:
        tasks_qs = tasks_qs.filter(
            Q(created_by=request.user)
            | Q(primary_responsible=request.user)
            | Q(responsibles=request.user)
        ).distinct()
    tasks = list(tasks_qs[:30])

    crm_items_qs = CrmItem.objects.filter(
        client=client,
        organization_id=client.organization_id,
        is_archived=False,
    ).select_related("pool", "responsible")
    if (
        not request.user.is_superuser
        and not (user_roles & {"owner", "admin", "manager"})
    ):
        crm_items_qs = crm_items_qs.filter(
            direction=CrmItem.DIRECTION_SERVICE
        )
    crm_items = list(crm_items_qs.order_by("-updated_at", "-id")[:20])

    calls_qs = client.phone_calls.select_related(
        "employee",
        "employee_profile",
        "analysis",
    ).order_by("-started_at", "-id")
    call_access = None
    if client.organization_id and not request.user.is_superuser:
        call_access = CommunicationAccess.objects.filter(
            organization_id=client.organization_id,
            user=request.user,
        ).first()
    if request.user.is_superuser or (call_access and call_access.can_view_all_calls):
        visible_calls = calls_qs
    elif call_access and call_access.can_view_own_calls:
        visible_calls = calls_qs.filter(employee=request.user)
    else:
        visible_calls = calls_qs.none()
    calls = list(visible_calls[:20])
    for call in calls:
        call.analysis_obj = getattr(call, "analysis", None)

    if client.client_type == "legal":
        related_people = list(
            client.person_links.select_related("person", "person__crm_profile")
            .order_by("-is_primary", "person__name", "id")
        )
        related_companies = []
    else:
        related_companies = list(
            client.company_links.select_related("company", "company__crm_profile")
            .order_by("-is_primary", "company__name", "id")
        )
        related_people = []

    staff_users = []
    if client.organization_id:
        staff_users = list(
            User.objects.filter(
                is_active=True,
                organizationaccess__organization_id=client.organization_id,
            )
            .distinct()
            .order_by("first_name", "last_name", "username")
        )

    active_task_count = sum(
        1
        for task in tasks
        if task.status not in {
            ServiceTask.STATUS_DONE,
            ServiceTask.STATUS_CANCELLED,
        }
    )

    return render(
        request,
        "pool_service/client_detail.html",
        {
            "page_title": client.name,
            "page_subtitle": "Карточка клиента",
            "active_tab": "clients",
            "client": client,
            "profile": profile,
            "contacts": contacts,
            "pools": pools,
            "tasks": tasks,
            "crm_items": crm_items,
            "calls": calls,
            "related_people": related_people,
            "related_companies": related_companies,
            "staff_users": staff_users,
            "can_manage_profile": can_manage_profile,
            "manager_label": _user_label(profile.manager) if profile else "",
            "responsible_label": _user_label(profile.responsible) if profile else "",
            "object_count": len(pools),
            "active_task_count": active_task_count,
            "call_count": visible_calls.count(),
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )


@login_required
def client_onec_import(request):
    try:
        organization_id = int(getattr(settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", "") or 0)
    except (TypeError, ValueError):
        organization_id = 0
    if not organization_id or not _can_manage_import(request.user, organization_id):
        return HttpResponseForbidden()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "scan":
            try:
                run, started = request_client_import_scan(request.user)
            except Exception:
                logger.exception("Failed to enqueue 1C client import")
                messages.error(
                    request,
                    "Не удалось запустить обновление из 1С. Попробуйте ещё раз после обновления страницы.",
                )
                return redirect("client_onec_import")
            if started:
                messages.success(
                    request,
                    "Обновление из 1С запущено. Можно закрыть страницу или открыть её на другом компьютере — процесс продолжится на сервере.",
                )
            elif run.status in {
                ClientImportRun.STATUS_PENDING,
                ClientImportRun.STATUS_RUNNING,
                ClientImportRun.STATUS_APPLYING,
            }:
                messages.info(
                    request,
                    (
                        "Импорт клиентов уже выполняется."
                        if run.status == ClientImportRun.STATUS_APPLYING
                        else "Обновление из 1С уже выполняется. Повторный запуск не создан."
                    ),
                )
            else:
                messages.error(
                    request,
                    run.error or "Не удалось запустить обновление из 1С.",
                )
            return redirect("client_onec_import")
        if action == "resolve":
            if ClientImportRun.objects.filter(
                organization_id=organization_id,
                status__in=[
                    ClientImportRun.STATUS_PENDING,
                    ClientImportRun.STATUS_RUNNING,
                    ClientImportRun.STATUS_APPLYING,
                ],
            ).exists():
                messages.warning(
                    request,
                    "Дождитесь завершения текущего обновления или импорта клиентов.",
                )
                return redirect("client_onec_import")
            try:
                candidate_id = int(request.POST.get("candidate_id") or 0)
            except (TypeError, ValueError):
                return HttpResponseForbidden()
            resolution = (request.POST.get("resolution") or "").strip()
            try:
                candidate = resolve_import_candidate(
                    candidate_id,
                    resolution,
                    request.user,
                )
            except (ClientImportCandidate.DoesNotExist, ValueError) as exc:
                messages.error(request, str(exc) or "Не удалось сохранить решение.")
            else:
                messages.success(
                    request,
                    f"{candidate.name}: решение сохранено.",
                )
            status = (request.POST.get("return_status") or "").strip()
            return_manual = (request.POST.get("return_manual") or "").strip() == "1"
            url = reverse("client_onec_import")
            if return_manual:
                url = f"{url}?manual=1"
            elif status in dict(ClientImportCandidate.STATUS_CHOICES):
                url = f"{url}?status={status}"
            return redirect(url)

        if action == "apply":
            try:
                run, started = request_client_apply()
            except ValueError as exc:
                messages.warning(request, str(exc))
            except Exception:
                logger.exception("Failed to enqueue 1C client apply")
                messages.error(
                    request,
                    "Не удалось запустить импорт клиентов. Обновите страницу и попробуйте ещё раз.",
                )
            else:
                if started:
                    messages.success(
                        request,
                        "Импорт готовых клиентов запущен на сервере. Можно закрыть страницу — процесс продолжится.",
                    )
                elif run.status == ClientImportRun.STATUS_APPLYING:
                    messages.info(request, "Импорт клиентов уже выполняется.")
                else:
                    messages.info(request, "Готовых карточек для импорта больше нет.")
            return redirect("client_onec_import")
        return HttpResponseForbidden()

    latest_run = (
        ClientImportRun.objects.filter(organization_id=organization_id)
        .order_by("-requested_at", "-id")
        .first()
    )
    import_active = bool(
        latest_run
        and latest_run.status in {
            ClientImportRun.STATUS_PENDING,
            ClientImportRun.STATUS_RUNNING,
            ClientImportRun.STATUS_APPLYING,
        }
    )

    candidates = ClientImportCandidate.objects.filter(
        organization_id=organization_id,
    ).select_related("matched_client")
    summary = {
        item["status"]: item["count"]
        for item in candidates.values("status").annotate(count=Count("id"))
    }
    kinds = {
        item["source_kind"]: item["count"]
        for item in candidates.values("source_kind").annotate(count=Count("id"))
    }
    current_status = request.GET.get("status", "").strip()
    manual_only = (request.GET.get("manual") or "").strip() == "1"
    if manual_only:
        candidates = candidates.exclude(
            resolution=ClientImportCandidate.RESOLUTION_AUTO
        )
        current_status = ""
    elif current_status in dict(ClientImportCandidate.STATUS_CHOICES):
        candidates = candidates.filter(status=current_status)
    else:
        current_status = ""

    manual_count = ClientImportCandidate.objects.filter(
        organization_id=organization_id
    ).exclude(resolution=ClientImportCandidate.RESOLUTION_AUTO).count()

    return render(
        request,
        "pool_service/client_onec_import.html",
        {
            "page_title": "Импорт клиентов из 1С",
            "page_subtitle": "Предпросмотр и безопасное сопоставление покупателей 1С с CRM",
            "active_tab": "clients",
            "summary": summary,
            "kinds": kinds,
            "current_status": current_status,
            "manual_only": manual_only,
            "manual_count": manual_count,
            "candidates": candidates.order_by("name")[:500],
            "total_candidates": ClientImportCandidate.objects.filter(
                organization_id=organization_id
            ).count(),
            "ready_count": summary.get(ClientImportCandidate.STATUS_READY, 0),
            "latest_run": latest_run,
            "import_active": import_active,
            "can_apply": bool(
                latest_run
                and latest_run.status == ClientImportRun.STATUS_SUCCESS
                and summary.get(ClientImportCandidate.STATUS_READY, 0)
            ),
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )



@login_required
def client_merge_index(request):
    try:
        organization_id = int(
            getattr(settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", "") or 0
        )
    except (TypeError, ValueError):
        organization_id = 0
    if not organization_id or not _can_manage_import(request.user, organization_id):
        return HttpResponseForbidden()

    if request.method == "POST":
        if ClientImportRun.objects.filter(
            organization_id=organization_id,
            status__in=[
                ClientImportRun.STATUS_PENDING,
                ClientImportRun.STATUS_RUNNING,
                ClientImportRun.STATUS_APPLYING,
            ],
        ).exists():
            messages.warning(
                request,
                "Дождитесь завершения обновления или импорта клиентов перед объединением.",
            )
            return redirect("client_merge_index")
        try:
            source_id = int(request.POST.get("source_id") or 0)
            target_id = int(request.POST.get("target_id") or 0)
        except (TypeError, ValueError):
            messages.error(request, "Некорректный выбор клиента.")
            return redirect("client_merge_index")
        try:
            result = merge_clients(
                source_id,
                target_id,
                organization_id=organization_id,
                actor=request.user,
            )
        except ValueError as exc:
            messages.error(request, str(exc))
        else:
            moved_total = sum(result["moved"].values())
            messages.success(
                request,
                f"Карточки объединены. Перенесено связанных записей: {moved_total}.",
            )
        return redirect("client_merge_index")

    merge_blocked = ClientImportRun.objects.filter(
        organization_id=organization_id,
        status__in=[
            ClientImportRun.STATUS_PENDING,
            ClientImportRun.STATUS_RUNNING,
            ClientImportRun.STATUS_APPLYING,
        ],
    ).exists()

    legacy_qs = (
        Client.objects.filter(
            organization_id=organization_id,
            pool__isnull=False,
        )
        .filter(
            Q(crm_profile__isnull=True)
            | Q(
                crm_profile__onec_ref__isnull=True,
                crm_profile__merged_into__isnull=True,
            )
        )
        .annotate(pool_count=Count("pool", distinct=True))
        .distinct()
        .order_by("name", "id")
    )

    canonical_qs = (
        Client.objects.filter(
            organization_id=organization_id,
            crm_profile__onec_ref__isnull=False,
            crm_profile__merged_into__isnull=True,
        )
        .select_related("crm_profile")
        .order_by("name", "id")
    )
    canonical = list(canonical_qs)

    selected_source = None
    source_raw = (request.GET.get("source") or "").strip()
    if source_raw:
        try:
            source_id = int(source_raw)
        except ValueError:
            source_id = 0
        if source_id:
            selected_source = legacy_qs.filter(pk=source_id).first()

    q = (request.GET.get("q") or "").strip()
    target_results = []
    suggestions = []
    if selected_source:
        if q:
            target_results = list(
                canonical_qs.filter(
                    Q(name__icontains=q)
                    | Q(company_name__icontains=q)
                    | Q(phone__icontains=q)
                    | Q(inn__icontains=q)
                )[:50]
            )
        else:
            suggestions = merge_suggestions(selected_source, canonical, limit=5)

    legacy_clients = list(legacy_qs)
    for client in legacy_clients:
        client.object_preview = list(
            Pool.objects.filter(client=client, is_deleted=False)
            .order_by("address", "id")
            .values_list("address", flat=True)[:3]
        )

    selected_pools = []
    if selected_source:
        selected_pools = list(
            Pool.objects.filter(client=selected_source, is_deleted=False)
            .order_by("address", "id")
        )

    return render(
        request,
        "pool_service/client_merge.html",
        {
            "page_title": "Объединение клиентов",
            "page_subtitle": "Перенос старых карточек Service2 на клиентов из 1С",
            "active_tab": "clients",
            "legacy_clients": legacy_clients,
            "selected_source": selected_source,
            "selected_pools": selected_pools,
            "suggestions": suggestions,
            "target_results": target_results,
            "q": q,
            "canonical_count": len(canonical),
            "merge_blocked": merge_blocked,
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )



@login_required
def client_merge_search(request):
    try:
        organization_id = int(
            getattr(settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", "") or 0
        )
    except (TypeError, ValueError):
        organization_id = 0
    if not organization_id or not _can_manage_import(request.user, organization_id):
        return HttpResponseForbidden()

    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})

    queryset = active_clients(
        Client.objects.filter(
            organization_id=organization_id,
            crm_profile__onec_ref__isnull=False,
        )
    ).filter(
        Q(name__icontains=q)
        | Q(company_name__icontains=q)
        | Q(phone__icontains=q)
        | Q(inn__icontains=q)
    ).order_by("name", "id")[:30]

    return JsonResponse(
        {
            "results": [
                {
                    "id": client.id,
                    "name": client.name,
                    "phone": client.phone or "",
                    "inn": client.inn or "",
                    "client_type": client.client_type,
                }
                for client in queryset
            ]
        }
    )
