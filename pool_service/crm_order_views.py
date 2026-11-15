"""Cached GET lookup and an explicitly requested exact-number source refresh."""
import json
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST
from .crm_live_views import _api, _organization, _deal_queryset, _id, MAX_BODY_BYTES
from .services.crm_order_finance import order_candidates, refresh_order_candidates, OrderRefreshUnavailable


@_api
@require_GET
def candidates(request):
    organization = _organization(request)
    deal = get_object_or_404(_deal_queryset(request.user, organization), pk=_id(request.GET.get('deal_id')))
    return JsonResponse(order_candidates(actor=request.user, organization=organization, deal=deal,
        query=request.GET.get('q', '').strip()[:100]))


@_api
@require_POST
def refresh(request):
    organization = _organization(request)
    if request.content_type != 'application/json':
        raise ValidationError('Ожидается запрос JSON.')
    try:
        if int(request.META.get('CONTENT_LENGTH') or 0) > MAX_BODY_BYTES or len(request.body) > MAX_BODY_BYTES:
            raise ValidationError('Слишком большой запрос.')
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError('Некорректный запрос JSON.') from exc
    if type(payload) is not dict or set(payload) != {'deal_id', 'q'} or type(payload['deal_id']) is not int:
        raise ValidationError('Укажите сделку и точный номер заказа.')
    deal = get_object_or_404(_deal_queryset(request.user, organization), pk=_id(payload['deal_id']))
    try:
        result = refresh_order_candidates(actor=request.user, organization=organization, deal_id=deal.pk, query=payload['q'])
    except OrderRefreshUnavailable:
        return JsonResponse({'error': 'Не удалось проверить заказ в 1С. Повторите поиск позже.',
            'code': 'source_unavailable'}, status=503)
    return JsonResponse(result)
