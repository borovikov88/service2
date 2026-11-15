"""Read-only, current permission scoped responsible choices."""
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from .crm_live_views import _api, _id
from .services.crm_assignments import responsible_candidates


@_api
@require_GET
def candidates(request):
    kind = request.GET.get('kind')
    if kind == 'lead':
        from .crm_lead_views import _organization
    else:
        from .crm_live_views import _organization
    organization = _organization(request)
    return JsonResponse(responsible_candidates(actor=request.user, organization=organization, kind=kind,
        subject_id=_id(request.GET['subject_id']) if 'subject_id' in request.GET else None,
        lead_id=_id(request.GET['lead_id']) if 'lead_id' in request.GET else None))
