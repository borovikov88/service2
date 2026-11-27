from django import template
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from pool_service.communication_services import selected_communication_access
from pool_service.mailbox_models import CrmMailbox
from pool_service.services.mailbox_access import mailbox_role
from pool_service.services.mailbox_routing import contact_letters

register = template.Library()


@register.simple_tag
def mailbox_navigation(request, org_id=None):
    if not request or not request.user.is_authenticated:
        return ""
    try:
        access = selected_communication_access(request)
    except PermissionDenied:
        return ""
    if not access or (org_id and str(org_id) != str(access.organization_id)):
        return ""
    if mailbox_role(request.user, access.organization):
        return f"{reverse('crm_mail')}?org_id={access.organization_id}"
    return ""


@register.inclusion_tag("pool_service/mailbox/_client.html")
def client_mail(request, client):
    if not client.organization_id or not mailbox_role(request.user, client.organization):
        return {}
    box = CrmMailbox.objects.filter(organization_id=client.organization_id).first()
    if not box:
        return {}
    letters = list(
        contact_letters(box, client).order_by("-sent_at", "-pk").only("pk", "subject", "sent_at")[:5]
    )
    return {"letters": letters, "client": client}
