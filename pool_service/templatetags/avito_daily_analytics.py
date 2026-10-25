from django import template
from pool_service.avito_daily_statistics import display

register = template.Library()


@register.inclusion_tag('pool_service/avito/_daily_statistics.html', takes_context=True)
def avito_daily_analytics(context, connection):
    request = context.get('request')
    return {'daily': display(connection, request.user) if request and connection else None,
            'connection': connection}
