"""Cached GET lookup and an explicitly requested exact-number source refresh."""
import json
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST
from .crm_live_views import _api, _organization, _deal_queryset, _id, MAX_BODY_BYTES
from .services.crm_order_finance import order_candidates, refresh_order_candidates, OrderRefreshUnavailable
from .services.crm_document_links import document_candidates, refresh_documents


@_api
@require_GET
def candidates(request):
    organization = _organization(request)
    deal = get_object_or_404(_deal_queryset(request.user, organization), pk=_id(request.GET.get('deal_id')))
    return JsonResponse(order_candidates(actor=request.user, organization=organization, deal=deal,
        query=request.GET.get('q', '').strip()[:100]))


def _json_body(request, fields):
    if request.content_type != 'application/json':
        raise ValidationError('Ожидается запрос JSON.')
    try:
        if int(request.META.get('CONTENT_LENGTH') or 0) > MAX_BODY_BYTES or len(request.body) > MAX_BODY_BYTES:
            raise ValidationError('Слишком большой запрос.')
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError('Некорректный запрос JSON.') from exc
    if type(payload) is not dict or set(payload) != fields or type(payload["deal_id"]) is not int:
        raise ValidationError("Укажите сделку и полный номер документа.")
    return payload


@_api
@require_POST
def refresh(request):
    organization = _organization(request)
    payload = _json_body(request, {"deal_id", "q"})
    deal = get_object_or_404(_deal_queryset(request.user, organization), pk=_id(payload['deal_id']))
    try:
        result = refresh_order_candidates(actor=request.user, organization=organization, deal_id=deal.pk, query=payload['q'])
    except OrderRefreshUnavailable:
        return JsonResponse({'error': 'Не удалось проверить заказ в 1С. Повторите поиск позже.',
            'code': 'source_unavailable'}, status=503)
    return JsonResponse(result)


@_api
@require_GET
def documents(request):
    organization = _organization(request)
    deal = get_object_or_404(_deal_queryset(request.user, organization), pk=_id(request.GET.get("deal_id")))
    return JsonResponse(document_candidates(actor=request.user, organization=organization, deal=deal,
        kind=request.GET.get("kind", "order"), query=request.GET.get("q", "").strip()[:100],
        amount=request.GET.get("amount", "")[:40], day=request.GET.get("date", "")[:20],
        page=_id(request.GET.get("page", "1"))))


@_api
@require_POST
def refresh_document(request):
    organization = _organization(request)
    payload = _json_body(request, {"deal_id", "kind", "q"})
    deal = get_object_or_404(_deal_queryset(request.user, organization), pk=_id(payload["deal_id"]))
    try:
        result = refresh_documents(actor=request.user, organization=organization, deal_id=deal.pk,
            kind=payload["kind"], query=payload["q"])
    except OrderRefreshUnavailable:
        return JsonResponse({"error": "1С не ответила. В поиске доступны ранее сохранённые данные.",
            "code": "source_unavailable"}, status=503)
    return JsonResponse(result)
