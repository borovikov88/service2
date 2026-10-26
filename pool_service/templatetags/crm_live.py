from django import template
from django.core.exceptions import PermissionDenied

register = template.Library()


def _navigation_options(request):
    """One request's advisory role/scope snapshot for both CRM menu entries.

    This cache is only consumed by template tags. Page discovery and every
    read/write endpoint continue to run their current authorization policies.
    """
    if not hasattr(request, "_crm_navigation_options"):
        from pool_service.crm_pilot_views import entry_options, explicit_organization
        from pool_service.services.permissions import organization_accesses_for_user

        try:
            organization = explicit_organization(request)
            options = entry_options(request.user,
                navigation_accesses=organization_accesses_for_user(request.user), include_leads=True)
            if organization is not None:
                options = [option for option in options if option["organization"].pk == organization.pk]
        except PermissionDenied:
            options = []
        request._crm_navigation_options = options
    return request._crm_navigation_options


@register.simple_tag(takes_context=True)
def crm_workspace_entry(context):
    """Reuse only an advisory navigation URL, never an authorization decision."""
    from django.urls import reverse

    request = context.get("request")
    if request is None:
        return ""
    if hasattr(request, "_crm_workspace_navigation_url"):
        return request._crm_workspace_navigation_url
    options = [option for option in _navigation_options(request) if option["url"]]
    url = options[0]["url"] if len(options) == 1 else (reverse("crm_live_workspace") if options else "")
    request._crm_workspace_navigation_url = url
    return url


@register.simple_tag(takes_context=True)
def crm_leads_entry(context):
    """Share the same advisory snapshot as the deals navigation entry."""
    request = context.get("request")
    if request is None or not request.user.is_authenticated:
        return ""
    if hasattr(request, "_crm_leads_navigation_url"):
        return request._crm_leads_navigation_url
    options = [option for option in _navigation_options(request) if option["leads_url"]]
    url = options[0]["leads_url"] if len(options) == 1 else ""
    request._crm_leads_navigation_url = url
    return url
