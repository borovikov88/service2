from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .models import Organization
from .services.crm_commercial_access import can_view_team_deals
from .services.crm_pilot_activation import activate_owner_pilot, can_activate_pilot


def entry_for(request, organization):
    if organization and can_view_team_deals(request.user, organization):
        return reverse("crm_live_workspace") + "?" + urlencode({"org_id": organization.pk})
    if organization and can_activate_pilot(request.user, organization):
        return reverse("crm_live_start") + "?" + urlencode({"org_id": organization.pk})
    return ""


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def start(request):
    selected = request.GET.getlist("org_id")
    if len(selected) != 1 or not selected[0].isascii() or not selected[0].isdigit() or len(selected[0]) > 18:
        raise PermissionDenied
    organization = Organization.objects.filter(pk=int(selected[0])).first()
    if not can_activate_pilot(request.user, organization):
        raise PermissionDenied
    if request.method == "POST":
        if request.POST.get("action") != "activate_self" or set(request.POST.keys()) - {"action", "csrfmiddlewaretoken"}:
            raise PermissionDenied
        organization = activate_owner_pilot(actor=request.user, organization_id=organization.pk)
        return redirect(reverse("crm_live_workspace") + "?" + urlencode({"org_id": organization.pk}))
    return render(request, "pool_service/crm_live/start.html", {
        "page_title": "Сделки", "active_tab": "crm", "organization": organization,
    })
