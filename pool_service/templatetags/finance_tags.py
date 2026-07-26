from django import template

from pool_service.services.finance import user_display_name


register = template.Library()


@register.filter
def finance_employee_name(user):
    if not user:
        return "—"
    return user_display_name(user)
