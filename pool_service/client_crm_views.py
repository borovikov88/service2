from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render

from .client_crm_import import apply_ready_candidates, scan_onec_clients
from .client_crm_models import ClientImportCandidate
from .models import OrganizationAccess


IMPORT_ROLES = {"owner", "admin"}


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
        organization_id = int(getattr(__import__("django.conf").conf.settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", "") or 0)
    except (TypeError, ValueError):
        organization_id = 0
    if not organization_id or not _can_manage_import(request.user, organization_id):
        return HttpResponseForbidden()

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "scan":
            try:
                result = scan_onec_clients()
            except Exception:
                messages.error(
                    request,
                    "Не удалось получить клиентов из 1С. Данные CRM не изменены.",
                )
            else:
                messages.success(
                    request,
                    "Данные 1С обновлены: найдено "
                    f"{result.get('total', 0)} покупателей.",
                )
            return redirect("client_onec_import")
        if action == "apply":
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
            "show_search": False,
            "show_add_button": False,
            "add_url": None,
        },
    )
