from django import template
from django.core.exceptions import PermissionDenied

register = template.Library()


@register.simple_tag(takes_context=True)
def crm_workspace_entry(context):
    """Reuse only an advisory navigation URL, never an authorization decision."""
    from django.urls import reverse
    from pool_service.crm_pilot_views import entry_options, explicit_organization
    from pool_service.services.permissions import organization_accesses_for_user

    request = context.get("request")
    if request is None:
        return ""
    if hasattr(request, "_crm_workspace_navigation_url"):
        return request._crm_workspace_navigation_url
    try:
        organization = explicit_organization(request)
        options = entry_options(request.user, navigation_accesses=organization_accesses_for_user(request.user))
        if organization is not None:
            url = next((option["url"] for option in options if option["organization"].pk == organization.pk), "")
        else:
            url = options[0]["url"] if len(options) == 1 else (reverse("crm_live_workspace") if options else "")
    except PermissionDenied:
        url = ""
    request._crm_workspace_navigation_url = url
    return url


@register.simple_tag(takes_context=True)
def crm_leads_entry(context):
    from urllib.parse import urlencode
    from django.urls import reverse
    from pool_service.crm_pilot_views import explicit_organization
    from pool_service.models import Organization
    from pool_service.services.crm_lead_access import can_view_leads

    request = context.get("request")
    if request is None or not request.user.is_authenticated:
        return ""
    if hasattr(request, "_crm_leads_navigation_url"):
        return request._crm_leads_navigation_url
    try:
        explicit = explicit_organization(request)
        candidates = Organization.objects.filter(accesses__user_id=request.user.pk,
            crm_v2_read=True, crmaccess__user_id=request.user.pk, crmaccess__view_leads=True).distinct()
        if explicit is not None:
            candidates = candidates.filter(pk=explicit.pk)
        options = [org for org in candidates.order_by("pk") if can_view_leads(request.user, org)]
        url = reverse("crm_live_leads") + "?" + urlencode({"org_id": options[0].pk}) if len(options) == 1 else ""
    except PermissionDenied:
        url = ""
    request._crm_leads_navigation_url = url
    return url
