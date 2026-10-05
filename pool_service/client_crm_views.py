import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from .client_crm_import import (
    request_client_apply,
    request_client_import_scan,
    resolve_import_candidate,
)
from .client_crm_models import ClientCRMProfile, ClientImportCandidate, ClientImportRun
from .client_merge import merge_clients, merge_suggestions
from .models import Client, OrganizationAccess, Pool


IMPORT_ROLES = {"owner", "admin"}
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
            result = merge_clients(source_id, target_id, request.user)
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
