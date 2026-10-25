from django import template

from pool_service.avito_autoload_presentation import for_display

register = template.Library()


@register.simple_tag
def avito_service_report(connection, report):
    return for_display(connection, report)
