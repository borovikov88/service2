from django import template
from django.core.exceptions import PermissionDenied

register = template.Library()


@register.simple_tag(takes_context=True)
def crm_workspace_entry(context):
    """Reuse only an advisory navigation URL, never an authorization decision."""
    from django.urls import reverse
    from pool_service.crm_pilot_views import entry_for, entry_options, explicit_organization

    request = context.get("request")
    if request is None:
        return ""
    if hasattr(request, "_crm_workspace_navigation_url"):
        return request._crm_workspace_navigation_url
    try:
        organization = explicit_organization(request)
        if organization is not None:
            url = entry_for(request, organization)
        else:
            options = entry_options(request.user)
            url = options[0]["url"] if len(options) == 1 else (reverse("crm_live_workspace") if options else "")
    except PermissionDenied:
        url = ""
    request._crm_workspace_navigation_url = url
    return url
