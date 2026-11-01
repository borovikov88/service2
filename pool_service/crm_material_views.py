from uuid import uuid4
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError,PermissionDenied
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponseBadRequest,HttpResponseNotAllowed
from django.shortcuts import render,redirect
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from .services import crm_materials as service
from .services.crm_deal_objects import object_querysets
from .services.crm_commercial_commands import CommercialConflict


def integer(value):
    if not str(value).isascii() or not str(value).isdigit() or len(str(value))>18 or int(value)<1:raise ValidationError('Некорректный идентификатор.')
    return int(value)


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def workspace(request,issue_id=None):
    try:
        if len(request.GET.getlist('org_id'))!=1:raise PermissionDenied
        org_id=integer(request.GET['org_id'])
    except (ValidationError,KeyError):raise PermissionDenied
    before=request.GET.get('before')
    try:before=integer(before) if before else None
    except ValidationError:return HttpResponseBadRequest('Некорректная страница.')
    if 'month' in request.GET and not issue_id:
        with transaction.atomic():service.authority(request.user,org_id)
        if request.method!='GET':return HttpResponseNotAllowed(['GET'])
        if len(request.GET.getlist('month'))!=1:return HttpResponseBadRequest('Укажите один месяц.')
        try:org,grant,groups,next_before=service.monthly(actor=request.user,organization_id=org_id,month=request.GET['month'],before=before)
        except ValidationError as exc:return HttpResponseBadRequest('; '.join(exc.messages))
        return render(request,'pool_service/crm_live/material_month.html',dict(organization=org,groups=groups,month=request.GET['month'],next_before=next_before))
    values=request.POST if request.method=='POST' else {}
    operation_key=values.get('operation_key') or uuid4().hex
    base=reverse('crm_materials')+'?org_id='+str(org_id)
    error='';status=200
    # Authenticate even malformed POSTs before returning validation information.
    with transaction.atomic():org,grant_user,grant=service.authority(request.user,org_id,write=request.method=='POST')
    if request.method=='POST':
        if len(request.POST.urlencode())>16384:return HttpResponseBadRequest('Слишком большой запрос.')
        try:
            if values.get('confirmed')!='on':raise ValidationError('Подтвердите фактическую запись.')
            if issue_id:
                service.move(actor=request.user,organization_id=org_id,issue_id=issue_id,operation_key=operation_key,
                    expected_revision=int(values.get('revision','-1')),kind=values.get('kind'),quantity=values.get('quantity'),
                    occurred_on=values.get('occurred_on'),source=values.get('source'),reason=values.get('reason',''),
                    pool_id=integer(values['pool_id']) if values.get('pool_id') else None,
                    deal_id=integer(values['deal_id']) if values.get('deal_id') else None,
                    reverses_id=integer(values['reverses_id']) if values.get('reverses_id') else None)
                row_id=issue_id
            else:
                row=service.issue(actor=request.user,organization_id=org_id,operation_key=operation_key,
                    pool_id=integer(values.get('pool_id')),deal_id=integer(values['deal_id']) if values.get('deal_id') else None,
                    item_name=values.get('item_name'),unit=values.get('unit'),packaging=values.get('packaging'),
                    quantity=values.get('quantity'),occurred_on=values.get('occurred_on'),source=values.get('source'))
                row_id=row.pk
            return redirect(reverse('crm_material_detail',args=[row_id])+'?org_id='+str(org_id))
        except PermissionDenied:raise
        except (ValidationError,ValueError,TypeError) as exc:
            status=409 if isinstance(exc,CommercialConflict) else 400
            error='; '.join(exc.messages) if isinstance(exc,ValidationError) else 'Проверьте поля записи.'
    context=dict(organization=org,base=base,error=error,values=values,key=operation_key,grant=grant,today=service.today())
    if issue_id:
        _,_,row,totals,history,restricted=service.read(actor=request.user,organization_id=org_id,issue_id=issue_id,before=before)
        context.update(issue=row,totals=totals,history=history[:20],restricted_history=restricted,next_before=history[19].pk if len(history)>20 else None)
    else:
        _,_,rows=service.read(actor=request.user,organization_id=org_id,before=before)
        context.update(rows=rows[:50],next_before=rows[49].pk if len(rows)>50 else None)
    pools,_=object_querysets(request.user,org_id)
    query=request.GET.get('q','').strip()[:120]
    if query:pools=pools.filter(Q(address__icontains=query)|Q(client__name__icontains=query))
    context['pools']=list(pools.select_related('client').order_by('pk')[:100]);context['q']=query
    selected=values.get('pool_id') or (context['issue'].pool_id if issue_id else None)
    if selected:
        try:selected=integer(selected)
        except ValidationError:selected=None
        if selected and all(p.pk!=selected for p in context['pools']):
            extra=object_querysets(request.user,org_id)[0].filter(pk=selected).first()
            if extra:context['pools'].append(extra)
    context['selected_pool']=str(selected or '')
    return render(request,'pool_service/crm_live/materials.html',context,status=status)
