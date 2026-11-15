"""Review within the existing 1C client import workspace; no live source reads."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponseForbidden
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .client_crm_models import ClientImportProposal
from .client_crm_views import _can_manage_import
from .client_import_review import LABELS, decide, token
from .models import Organization
from .services.permissions import company_has_access


@login_required
@require_http_methods(["GET", "POST"])
def proposals(request):
    try:
        org_id = int(getattr(settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", "") or 0)
    except (TypeError, ValueError):
        org_id = 0
    org = Organization.objects.filter(pk=org_id).first()
    if not org or not _can_manage_import(request.user, org_id) or not company_has_access(org):
        return HttpResponseForbidden()
    response_status = 200
    state_override = None
    if request.method == "POST":
        try:
            row = decide(actor=request.user, organization_id=org_id,
                proposal_id=int(request.POST.get("proposal_id", "0")),
                decision=request.POST.get("decision"), expected_token=request.POST.get("token"))
        except PermissionDenied:
            return HttpResponseForbidden()
        except (ValueError, ValidationError):
            response_status = 409
            messages.error(request, "Решение не применено. Проверьте связи клиента и обновите страницу.")
        else:
            if row.state == "stale":
                response_status = 409
                state_override = "stale"
                messages.warning(request, "Предложение устарело: данные или источник изменились. Обновите сравнение.")
            else:
                messages.success(request, "Сравнение обновлено." if row.state == "pending" else "Решение сохранено.")
        if response_status == 200:
            return redirect("client_onec_proposals")
    state = state_override or request.GET.get("state", "pending")
    if state not in dict(ClientImportProposal._meta.get_field("state").choices):
        state = "pending"
    rows = ClientImportProposal.objects.filter(candidate__organization=org, client__organization=org,
        state=state).select_related("candidate", "client", "decided_by").order_by("-created_at", "-pk")
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    for row in page:
        row.token = token(row)
        row.removes_ip_links = row.field == "classification" and row.after["legal_form"] != "ip" and any(
            link["automatic"] and link["source"] == "onec_ip" and link["source_reference"] == row.context["source_ref"]
            and link["company_id"] == row.client_id for link in row.before["links"])
        row.person_values = [(LABELS.get(key, "Дополнительный контакт"), display(value, key))
            for key, value in row.after.get("values", {}).items() if value] if row.field == "ip_person" else []
        row.label = ("Дополнительный телефон" if row.field.startswith("contact:phone:") else
            "Дополнительный email" if row.field.startswith("contact:email:") else "Подпись контакта" if row.field.startswith("contact_label:") else LABELS.get(row.field, row.field))
        row.before_display = display(row.before, row.field, before=True)
        row.after_display = display(row.after, row.field)
    return render(request, "pool_service/client_onec_proposals.html", dict(page=page, state=state,
        states=ClientImportProposal._meta.get_field("state").choices, active_tab="clients",
        page_title="Предложения изменений из 1С", show_search=False, show_add_button=False), status=response_status)


def display(value, field, *, before=False):
    if field == "classification":
        return {"private": "Физическое лицо", "legal": "Компания"}.get(value["client_type"], "") + (
            " · ИП" if value["legal_form"] == "ip" else "")
    if field == "ip_person":
        return "Связей: " + str(len(value)) if before else value["name"]
    if field.startswith("contact_label:"):
        return ((value["value"] + ": " + (value["label"] or "Не заполнено")) if before else value)
    if field.startswith("contact:"):
        if before:
            return "; ".join(filter(None, [value["primary"], *[r["value"] for r in value["rows"]]])) or "Не заполнено"
        return value["value"]
    return value or "Не заполнено"
