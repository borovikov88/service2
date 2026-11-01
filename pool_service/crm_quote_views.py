"""Server-rendered bounded proposal lifecycle; no provider sending or task creation."""
from uuid import uuid4
from zoneinfo import ZoneInfo
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from .crm_live_views import _organization
from .crm_commercial_models import CrmAccess
from .crm_quote_models import CrmQuote
from .estimate_models import PoolEstimateVersion
from .services import crm_quotes as service
from .services.crm_commercial_commands import CommercialConflict
from .services.permissions import company_has_access
from .services.pool_estimates import VARIANTS

SALT='crm-quote-preview-v1'
ACTION_LABELS={'prepare':'Подготовлено','send':'Отметка отправки','accept':'Принятие','reject':'Отказ','replace':'Замена'}
STATUS_LABELS=dict(CrmQuote._meta.get_field('status').choices)


def _integer(value):
    if not value or not str(value).isascii() or not str(value).isdigit() or int(value)<=0:
        raise ValidationError('Некорректный идентификатор.')
    return int(value)


def _rows(snapshot,variant):
    return service.selected_rows(snapshot,variant)[:300]


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def workspace(request,deal_id):
    org=_organization(request)
    deal=service.read_deal(request.user,org,deal_id)
    base=reverse('crm_quotes',args=[deal.pk])+'?org_id='+str(org.pk)
    status=200; error=''
    if request.method=='POST':
        if len(request.POST.urlencode())>16384: return HttpResponseBadRequest('Слишком большой запрос.')
        try:
            action=request.POST.get('action')
            revision=int(request.POST.get('revision','-1'))
            data={}
            if action=='prepare':
                token=signing.loads(request.POST.get('selection',''),salt=SALT,max_age=1800)
                if token['actor']!=request.user.pk or token['org']!=org.pk or token['deal']!=deal.pk: raise PermissionDenied
                revision=token['revision']
                data={'version_id':token['version'],'variant':token['variant'],'source_hash':token['hash'],'confirm_scope':request.POST.get('confirm_scope')=='on'}
            else:
                data={'quote_id':_integer(request.POST.get('quote_id')),'evidence':request.POST.get('evidence','')}
                if action=='send':
                    data.update({key:request.POST.get(key,'') for key in ('recipient','channel','occurred_at','outcome','external_message_id')})
                    stamp=parse_datetime(data['occurred_at'])
                    if stamp and timezone.is_naive(stamp): data['occurred_at']=timezone.make_aware(stamp,ZoneInfo('Asia/Barnaul')).isoformat()
                elif action=='accept':
                    data.update(confirm_amount=request.POST.get('confirm_amount')=='on',replace_quote_id=_integer(request.POST['replace_quote_id']) if request.POST.get('replace_quote_id') else None)
                elif action=='replace': data['replacement_id']=_integer(request.POST.get('replacement_id'))
            result=service.execute(actor=request.user,organization_id=org.pk,deal_id=deal.pk,operation_key=request.POST.get('operation_key',''),expected_revision=revision,action=action,data=data)
            return redirect(base+'&saved='+str(result['quote_id']))
        except PermissionDenied: raise
        except (ValidationError,signing.BadSignature,ValueError,KeyError) as exc:
            status=409 if isinstance(exc,CommercialConflict) else 400
            error='; '.join(exc.messages) if isinstance(exc,ValidationError) else 'Некорректное или устаревшее подтверждение. Обновите страницу.'
            deal.refresh_from_db()
    writable=(deal.manager_id==request.user.pk and org.crm_v2_write and not deal.is_archived and deal.outcome=='open'
        and company_has_access(org) and CrmAccess.objects.filter(organization=org,user=request.user,edit_own_deals=True).exists())
    versions=Paginator(PoolEstimateVersion.objects.filter(estimate__organization=org).select_related('estimate').defer('snapshot').order_by('-pk'),20).get_page(request.GET.get('versions_page'))
    quotes=Paginator(CrmQuote.objects.filter(deal=deal).select_related('version__estimate').order_by('-pk'),20).get_page(request.GET.get('page'))
    current=CrmQuote.objects.filter(deal=deal,status='accepted').first()
    for quote in quotes:
        quote.blocked='';quote.rows=[];quote.history=list(quote.events.select_related('actor').order_by('-pk')[:30]);quote.ready=False
        for event in quote.history:
            event.action_label=ACTION_LABELS.get(event.action,'Решение')
            event.before_label=STATUS_LABELS.get(event.before,'Начало')
            event.after_label=STATUS_LABELS.get(event.after,'Неизвестный статус')
            event.outcome_label={'unknown':'Результат неизвестен','sent':'Отправлено — ручная отметка'}.get(event.evidence.get('outcome'),'')
        try:
            snapshot=service.source(quote,deal)
            quote.rows=_rows(snapshot,quote.variant)
            quote.ready=quote.status=='accepted' and not deal.is_archived and deal.outcome=='open' and not service.readiness(snapshot,quote.variant)
            quote.document_url=reverse('pool_estimate_document',args=[quote.version.estimate_id,quote.version.number])+'?variants='+quote.variant
        except (ValidationError,KeyError,TypeError) as exc:
            quote.blocked='; '.join(exc.messages) if isinstance(exc,ValidationError) else 'Формат источника требует сверки.'
        quote.actionable=writable and quote.status in service.ACTIVE and not quote.blocked
        quote.keys={action:uuid4().hex for action in ('send','accept','reject','replace')}
    preview=None
    if request.GET.get('version') and writable:
        try:
            variant=request.GET.get('variant','economy')
            if variant not in VARIANTS: raise ValidationError('Выберите вариант.')
            version=PoolEstimateVersion.objects.select_related('estimate').filter(pk=_integer(request.GET['version']),estimate__organization=org).first()
            if not version: raise PermissionDenied
            preview={'version':version,'variant':variant,'amount':service._amount(version,variant),
                'rows':_rows(version.snapshot,variant),'documents':version.snapshot.get('documents',{}),
                'errors':service.readiness(version.snapshot,variant),
                'selection':signing.dumps({'actor':request.user.pk,'org':org.pk,'deal':deal.pk,'revision':deal.revision,'version':version.pk,'variant':variant,'hash':service.digest(version.snapshot)},salt=SALT),
                'key':uuid4().hex}
        except (ValidationError,KeyError,TypeError) as exc:
            error='; '.join(exc.messages) if isinstance(exc,ValidationError) else 'Версия расчёта неполна.';status=400
    return render(request,'pool_service/crm_live/quotes.html',dict(organization=org,deal=deal,base=base,
        writable=writable,quotes=quotes,versions=versions,variants=VARIANTS,preview=preview,current=current,error=error),status=status)
