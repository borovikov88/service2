import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from .client_crm_import import apply_ready_candidates, request_client_import_scan
from .client_crm_models import ClientImportCandidate, ClientImportRun
from .models import OrganizationAccess


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
            }:
                messages.info(
                    request,
                    "Обновление из 1С уже выполняется. Повторный запуск не создан.",
                )
            else:
                messages.error(
                    request,
                    run.error or "Не удалось запустить обновление из 1С.",
                )
            return redirect("client_onec_import")
        if action == "apply":
            active_run = ClientImportRun.objects.filter(
                organization_id=organization_id,
                status__in=[
                    ClientImportRun.STATUS_PENDING,
                    ClientImportRun.STATUS_RUNNING,
                ],
            ).exists()
            if active_run:
                messages.warning(
                    request,
                    "Сначала дождитесь завершения обновления из 1С.",
                )
                return redirect("client_onec_import")
            result = apply_ready_candidates()
            if result["failed"]:
                messages.warning(
                    request,
                    f"Импортировано: {result['imported']}. "
                    f"Требуют проверки: {result['failed']}.",
                )
            else:
                messages.success(
                    request,
                    f"Импортировано карточек: {result['imported']}.",
                )
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
    if current_status in dict(ClientImportCandidate.STATUS_CHOICES):
        candidates = candidates.filter(status=current_status)
    else:
        current_status = ""

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
            "candidates": candidates.order_by("name")[:500],
            "total_candidates": ClientImportCandidate.objects.filter(
                organization_id=organization_id
            ).count(),
            "ready_count": summary.get(ClientImportCandidate.STATUS_READY, 0),
            "latest_run": latest_run,
            "import_active": import_active,
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )
