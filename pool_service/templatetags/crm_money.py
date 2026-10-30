from django import template
from pool_service.crm_money import display_money
register = template.Library()
register.filter('crm_money', display_money)
