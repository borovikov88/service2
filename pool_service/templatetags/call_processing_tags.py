from django import template
from pool_service.services.permissions import organization_for_user
from pool_service.services.call_processing_settings import settings_allowed

register = template.Library()


@register.simple_tag(takes_context=True)
def call_processing_settings_allowed(context):
    request = context.get("request")
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated or not user.is_active:
        return False
    return settings_allowed(user, organization_for_user(user))
