"""The manual distribution queue uses the journal's current source permissions."""
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .communication_models import PhoneCall
from .communication_views import _selected_access
from .services.crm_commercial_access import can_view_team_deals
from .services.crm_communication_routing import assign_route, customer_deals, editable_targets, pending_routes, readable_routes, route_source
from .services.crm_lead_identity import source_identity
from .services.crm_role_runtime import resolve_crm_capabilities


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def distribution(request, route_id=None):
    access = _selected_access(request)
    if not access or not can_view_team_deals(request.user, access.organization):
        raise PermissionDenied
    organization = access.organization
    scope = urlencode({"org_id": organization.pk})
    queue_url = reverse("crm_communication_queue") + "?" + scope
    queryset = readable_routes(request.user, organization)
    route = get_object_or_404(queryset.select_related("inbox"), pk=route_id) if route_id else None
    can_assign = organization.crm_v2_write and resolve_crm_capabilities(request.user.pk, organization.pk).edit_own_deals
    if request.method == "POST":
        if not route or not can_assign:
            raise PermissionDenied
        try:
            raw_deal, raw_revision = request.POST.get("deal", ""), request.POST.get("revision", "")
            if not all(v.isascii() and v.isdigit() and len(v) <= 18 for v in (raw_deal, raw_revision)):
                raise ValidationError("Выберите сделку и обновите страницу, если связь изменилась.")
            assign_route(user=request.user, organization=organization, route_id=route.pk, deal_id=int(raw_deal),
                revision=int(raw_revision), remember_dialog=request.POST.get("remember_dialog") == "1")
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, "Обращение связано со сделкой.")
            return redirect(queue_url)
    query = request.GET.get("q", "").strip()[:100]
    source, targets, rows, next_page = None, [], [], None
    if route:
        source = route_source(route)
        if can_assign and source:
            candidates = editable_targets(request.user, organization)
            identity = source_identity(organization, source)
            if identity.client and not identity.ambiguous:
                candidates = customer_deals(candidates, identity)
            if query:
                candidates = candidates.filter(Q(title__icontains=query) | Q(client__name__icontains=query))
            elif identity.ambiguous or not identity.client:
                candidates = candidates.none()
            targets = list(candidates.order_by("-pk").values("pk", "title")[:30])
        if not queryset.filter(pk=route.pk).exists():
            raise PermissionDenied
    else:
        before = request.GET.get("before", "")
        pending = pending_routes(queryset, organization)
        if before.isascii() and before.isdigit() and len(before) <= 18:
            pending = pending.filter(pk__lt=int(before))
        rows = list(pending.select_related("inbox").order_by("-pk")[:31])
        if len(rows) > 30:
            rows = rows[:30]
            next_page = rows[-1].pk
    return render(request, "pool_service/communications/distribution.html", {
        "communication_org_id": organization.pk, "queue_url": queue_url, "route": route,
        "source": source, "is_call": isinstance(source, PhoneCall), "can_assign": can_assign,
        "targets": targets, "rows": rows, "next_page": next_page, "q": query,
        "show_distribution": True,
    })
