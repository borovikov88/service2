from datetime import date
from decimal import Decimal,InvalidOperation
from uuid import uuid4
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError,PermissionDenied
from django.http import HttpResponseBadRequest
from django.shortcuts import render,redirect
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from .crm_live_views import _organization
from .crm_recurring_models import CrmRecurringDraft,CrmRecurringRevision,CrmRecurringConfirmation
from .services import crm_recurring_drafts as service
from .services.crm_recurring_terms import ServicePeriodDraft
from .services.crm_commercial_commands import CommercialConflict
from .models import Pool
from .client_queries import readable_clients


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def workspace(request,deal_id):
    org=_organization(request)
    try:
        _,_,deal=service._scope_read_ui(request.user,org.pk,deal_id)
    except ValidationError:
        return _blocked(request, org, deal_id)
    base=reverse('crm_recurring',args=[deal.pk])+'?org_id='+str(org.pk)
    error='';status=200
    if request.method=='POST':
        if len(request.POST.urlencode())>131072:return HttpResponseBadRequest('Слишком большой запрос.')
        try:
            if request.POST.get('action')=='confirm':
                service.confirm(actor=request.user,organization_id=org.pk,deal_id=deal.pk,
                    revision_id=int(request.POST['revision_id']),expected_revision=int(request.POST['expected_revision']),
                    effective_from=date.fromisoformat(request.POST['effective_from']),confirm_effect=request.POST.get('confirm_effect')=='on')
            else:
                cols=[request.POST.getlist(k) for k in ('month','quantity','unit','amount')]
                if len({len(c) for c in cols})!=1:raise ValidationError('Неполная таблица периодов.')
                periods=[]
                for month,quantity,unit,amount in zip(*cols):
                    if not any((month,quantity,unit,amount)):continue
                    periods.append(ServicePeriodDraft(date.fromisoformat(month+'-01'),Decimal(quantity.replace(',','.')),unit,Decimal(amount.replace(',','.')) if amount.strip() else None))
                service.save_draft(actor=request.user,organization_id=org.pk,deal_id=deal.pk,
                    expected_deal_revision=int(request.POST['deal_revision']),expected_revision=int(request.POST['expected_revision']),
                    operation_key=request.POST['operation_key'],action=request.POST['action'],contract_basis=request.POST['contract_basis'],
                    payer_id=int(request.POST['payer_id']),reason=request.POST['reason'],same_contract_confirmed=request.POST.get('same_contract')=='on',
                    terms=dict(start=date.fromisoformat(request.POST['start']),end=date.fromisoformat(request.POST['end']) if request.POST.get('end') else None,
                        complete_period_list=request.POST.get('complete')=='on',renewal_review_on=date.fromisoformat(request.POST['review_on']),periods=periods),
                    schedule=dict(frequency=request.POST.get('frequency') or None,first_visit=date.fromisoformat(request.POST['first_visit']) if request.POST.get('first_visit') else None))
            return redirect(base)
        except PermissionDenied:raise
        except (ValidationError,ValueError,KeyError,InvalidOperation) as exc:
            error='; '.join(exc.messages) if isinstance(exc,ValidationError) else 'Проверьте даты, объёмы и суммы.'
            status=409 if isinstance(exc,CommercialConflict) else 400
    before=request.GET.get('before')
    if before is not None and (not before.isascii() or not before.isdigit() or len(before)>9 or int(before)<1):
        return HttpResponseBadRequest('Некорректная страница истории.')
    try:
        history=service.read_history(actor=request.user,organization_id=org.pk,deal_id=deal.pk,before_revision=int(before) if before else None)
    except ValidationError:
        # Eligibility may change between the two independently locked reads.
        return _blocked(request, org, deal_id)
    draft=CrmRecurringDraft.objects.filter(deal=deal).first()
    latest=CrmRecurringRevision.objects.filter(draft=draft).order_by('-revision').first() if draft else None
    confirmations={c.revision_id:c for c in CrmRecurringConfirmation.objects.filter(revision_id__in=[r.pk for r in history])} if draft else {}
    for row in history:row.confirmation=confirmations.get(row.pk)
    rows=latest.payload['periods'] if latest else []
    if request.method=='POST' and error and request.POST.get('action')!='confirm':
        rows=[dict(month=m+'-01',quantity=q,unit=u,agreed_amount=a) for m,q,u,a in zip(*[request.POST.getlist(k) for k in ('month','quantity','unit','amount')])]
    payload=latest.payload if latest else {}
    values=dict(contract_basis=draft.contract_basis if draft else '', start=payload.get('start',''),end=payload.get('end') or '',
        review_on=payload.get('renewal_review_on',''),frequency=payload.get('schedule',{}).get('frequency') or '',
        first_visit=payload.get('schedule',{}).get('first_visit') or '',deal_revision=deal.revision,
        expected_revision=latest.revision if latest else 0,reason='',complete=payload.get('complete_period_list',False),
        payer_id=str(draft.payer_id if draft else deal.client_id),action='revise' if draft else 'prepare')
    if error and request.POST.get('action')!='confirm':
        for name in values:
            if name=='complete':values[name]=request.POST.get(name)=='on'
            elif name in request.POST:values[name]=request.POST[name]
    payers=list(readable_clients(request.user).filter(organization=org).order_by('pk')[:100])
    selected=readable_clients(request.user).filter(organization=org,pk=values['payer_id']).first() if str(values['payer_id']).isdigit() else None
    if selected and all(p.pk!=selected.pk for p in payers):payers.append(selected)
    return render(request,'pool_service/crm_live/recurring.html',dict(organization=org,deal=deal,base=base,error=error,
        values=values,draft=draft,latest=latest,history=history,rows=rows or [dict(month='',quantity='',unit='',agreed_amount='')],
        payers=payers,key=uuid4().hex,
        frequencies=Pool.SERVICE_FREQUENCY_CHOICES,next_before=history[-1].revision if len(history)==20 else None),status=status)


def _blocked(request, organization, deal_id):
    # Authorization failures remain 403. Never expose raw validation exceptions,
    # related object data or a form capable of writing an ineligible contract.
    return render(request, 'pool_service/crm_live/recurring_blocked.html', {
        'workspace_url': reverse('crm_live_workspace') + '?org_id=' + str(organization.pk) + '&deal=' + str(deal_id),
    }, status=409 if request.method == 'POST' else 200)
