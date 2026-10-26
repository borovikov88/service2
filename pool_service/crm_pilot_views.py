from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Exists, OuterRef, Q
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .crm_commercial_models import CrmAccess
from .models import Employee, Organization, OrganizationAccess
from .services.crm_commercial_access import can_view_team_deals
from .services.crm_pilot_activation import activate_owner_pilot, can_activate_pilot


def entry_for(request, organization):
    if organization and can_view_team_deals(request.user, organization):
        return reverse("crm_live_workspace") + "?" + urlencode({"org_id": organization.pk})
    if organization and can_activate_pilot(request.user, organization):
        return reverse("crm_live_start") + "?" + urlencode({"org_id": organization.pk})
    return ""


def entry_options(user):
    """Bounded-query advisory choices; selected pages recheck canonical policy.

    Keep these predicates aligned with can_view_team_deals/can_activate_pilot.
    This list never authorizes an API request or an activation.
    """
    if not user or not user.is_authenticated or not get_user_model().objects.filter(pk=user.pk, is_active=True).exists():
        return []
    memberships = OrganizationAccess.objects.filter(user_id=user.pk, organization_id=OuterRef("pk"))
    blocked_employees = Employee.objects.filter(user_id=user.pk, organization_id=OuterRef("pk")).filter(
        Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED))
    organizations = Organization.objects.filter(accesses__user_id=user.pk).distinct().annotate(
        entry_owner=Exists(memberships.filter(role="owner")),
        entry_manager=Exists(memberships.filter(role="manager")),
        entry_grant=Exists(CrmAccess.objects.filter(user_id=user.pk, organization_id=OuterRef("pk"), view_team_deals=True)),
        entry_blocked=Exists(blocked_employees),
    ).filter(entry_blocked=False).filter(
        Q(entry_owner=True) | (Q(crm_v2_read=True) & (Q(entry_manager=True) | Q(entry_grant=True)))
    ).order_by("pk")
    return [{"organization": organization,
        "url": reverse("crm_live_workspace" if organization.crm_v2_read and (
            organization.entry_manager or organization.entry_grant) else "crm_live_start")
            + "?" + urlencode({"org_id": organization.pk})} for organization in organizations]


def explicit_organization(request):
    """Missing scope is a GET entry choice only; malformed scope never falls back."""
    selected = request.GET.getlist("org_id")
    if not selected:
        return None
    if len(selected) != 1 or not selected[0].isascii() or not selected[0].isdigit() or len(selected[0]) > 18 or int(selected[0]) <= 0:
        raise PermissionDenied
    organization = Organization.objects.filter(pk=int(selected[0])).first()
    if organization is None:
        raise PermissionDenied
    return organization


def choose_entry(request):
    options = entry_options(request.user)
    if not options:
        raise PermissionDenied
    if len(options) == 1:
        return redirect(options[0]["url"])
    return render(request, "pool_service/crm_live/choose.html", {
        "page_title": "Сделки", "active_tab": "crm", "entry_options": options,
    })


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def start(request):
    organization = explicit_organization(request)
    if organization is None:
        if request.method == "POST":
            raise PermissionDenied
        return choose_entry(request)
    if not can_activate_pilot(request.user, organization):
        raise PermissionDenied
    if request.method == "POST":
        if request.POST.get("action") != "activate_self" or set(request.POST.keys()) - {"action", "csrfmiddlewaretoken"}:
            raise PermissionDenied
        organization = activate_owner_pilot(actor=request.user, organization_id=organization.pk)
        return redirect(reverse("crm_live_workspace") + "?" + urlencode({"org_id": organization.pk}))
    return render(request, "pool_service/crm_live/start.html", {
        "page_title": "Сделки", "active_tab": "crm", "organization": organization,
        "activation_url": reverse("crm_live_start") + "?" + urlencode({"org_id": organization.pk}),
    })
