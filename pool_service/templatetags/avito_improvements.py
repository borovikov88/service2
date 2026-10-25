from django import template

register = template.Library()


@register.filter
def improvement_project(value):
    from pool_service.avito_improvement_proposals import is_proposal
    return is_proposal(value)
