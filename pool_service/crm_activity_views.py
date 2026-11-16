"""Activity reads share each workspace's current subject authority."""
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from . import crm_live_views as deals, crm_lead_views as leads
from .services.crm_card_activity import activity_page
from .services.crm_lead_access import can_read_lead


@deals._api
@require_GET
def deal_activity(request, deal_id):
    organization = deals._organization(request)
    subject = get_object_or_404(deals._deal_queryset(request.user, organization), pk=deal_id)
    result = activity_page(request, organization, subject)
    # Revalidate the card as well as each source after the split reads.
    deals._organization(request)
    if not deals._deal_queryset(request.user, organization).filter(pk=deal_id).exists():
        raise Http404
    return JsonResponse(result)


@leads._api
@require_GET
def lead_activity(request, lead_id):
    organization = leads._organization(request)
    subject = get_object_or_404(leads._queryset(request.user, organization), pk=lead_id)
    if not can_read_lead(request.user, organization, subject):
        raise Http404
    result = activity_page(request, organization, subject)
    leads._organization(request)
    if not can_read_lead(request.user, organization, subject):
        raise Http404
    return JsonResponse(result)
