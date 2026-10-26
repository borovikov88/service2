from django import template
from django.core.exceptions import PermissionDenied

register = template.Library()


@register.simple_tag(takes_context=True)
def crm_workspace_entry(context):
    """The legacy CRM overview links to the scoped, explicitly enabled pilot."""
    from pool_service.crm_pilot_views import entry_for
    from pool_service.views import _crm_get_org_for_request

    request = context.get("request")
    if request is None:
        return ""
    try:
        organization = _crm_get_org_for_request(request)
    except PermissionDenied:
        return ""
    return entry_for(request, organization)
