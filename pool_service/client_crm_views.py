import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from .client_crm_import import (
    CRM_SYNC_FEATURE,
    apply_ready_candidates,
    request_client_import_scan,
)
from .client_crm_models import ClientImportCandidate
from .models import OneCODataSyncRun, OrganizationAccess


IMPORT_ROLES = {"owner", "admin"}
logger = logging.getLogger(__name__)


def _latest_client_import_run(organization_id):
    for run in (
        OneCODataSyncRun.objects.filter(organization_id=organization_id)
        .order_by("-created_at", "-id")[:50]
    ):
        if isinstance(run.sync_scope, dict) and run.sync_scope.get("feature") == CRM_SYNC_FEATURE:
            return run
    return None


def _decorate_import_run(run):
    if run is None:
        return None
    progress = run.progress if isinstance(run.progress, dict) else {}
    summary = run.result_summary if isinstance(run.result_summary, dict) else {}
    run.total_rows = int(progress.get("total_rows") or summary.get("total") or 0)
    run.processed_rows = int(progress.get("processed_rows") or 0)
    run.ready_count = int(summary.get("ready_count") or progress.get("ready_count") or 0)
    run.review_count = int(summary.get("review_count") or progress.get("review_count") or 0)
    run.duplicate_count = int(summary.get("duplicate_count") or progress.get("duplicate_count") or 0)
    run.invalid_count = int(summary.get("invalid_count") or progress.get("invalid_count") or 0)
    run.imported_count = int(summary.get("imported_count") or progress.get("imported_count") or 0)
    run.error = run.error_message
    return run


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
                OneCODataSyncRun.STATUS_PENDING,
                OneCODataSyncRun.STATUS_RUNNING,
            }:
                messages.info(
                    request,
                    "Обновление из 1С уже выполняется. Повторный запуск не создан.",
                )
            else:
                messages.error(
                    request,
                    run.error_message or "Не удалось запустить обновление из 1С.",
                )
            return redirect("client_onec_import")
        if action == "apply":
            latest_run = _latest_client_import_run(organization_id)
            if not latest_run or latest_run.status != OneCODataSyncRun.STATUS_COMPLETED:
                messages.warning(
                    request,
                    "Импортировать карточки можно только после полностью успешного обновления из 1С.",
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

    latest_run = _decorate_import_run(_latest_client_import_run(organization_id))
    import_active = bool(
        latest_run
        and latest_run.status in {
            OneCODataSyncRun.STATUS_PENDING,
            OneCODataSyncRun.STATUS_RUNNING,
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
            "can_apply": bool(
                latest_run
                and latest_run.status == OneCODataSyncRun.STATUS_COMPLETED
                and summary.get(ClientImportCandidate.STATUS_READY, 0)
            ),
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )
