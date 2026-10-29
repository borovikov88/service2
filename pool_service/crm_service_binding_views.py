"""Browser supplies selections only; document amounts always come from a fresh server read."""
from datetime import date
from decimal import Decimal,InvalidOperation
from uuid import uuid4
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied,ValidationError
from django.http import HttpResponseBadRequest
from django.shortcuts import render,redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from . import onec_diagnostic
from .crm_live_views import _organization
from .crm_service_binding_models import ServiceBoundDocument
from .services import crm_service_binding_reader as reader
from .services.crm_document_allocations import Allocation
from .services.crm_service_bindings import save_allocations,observe_document

SALT='crm-service-document-selection-v1'
REFS=('document_ref','organization_ref','client_ref','contract_ref')


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def workspace(request,deal_id):
    org=_organization(request)
    actor,org,deal=reader.access(request.user,org.pk,deal_id)
    allowed=onec_diagnostic.can_access_diagnostic_mcp(actor,org)
    docs=ServiceBoundDocument.objects.filter(contract__deal=deal,contract__client_id=deal.client_id,
        contract__pool_id=deal.pool_id,contract__client_object_id=deal.client_object_id).select_related('active_version').order_by('-pk')[:20]
    context={'organization':org,'deal':deal,'documents':docs,'allowed':allowed,'error':'','snapshot':None}
    status=200
    if request.method=='POST':
        if not allowed: raise PermissionDenied
        if len(request.POST.urlencode())>32768: return HttpResponseBadRequest('Слишком большой запрос.')
        try:
            action=request.POST.get('action')
            if action=='preview':
                refs={key:request.POST.get(key,'') for key in REFS}
                snapshot=reader.read_document(actor=actor,organization_id=org.pk,deal_id=deal.pk,**refs)
                actor,org,deal=reader.access(actor,org.pk,deal.pk)
                if not onec_diagnostic.can_access_diagnostic_mcp(actor,org): raise PermissionDenied
                observe_document(actor=actor,organization_id=org.pk,deal_id=deal.pk,snapshot=snapshot)
            elif action=='save':
                token=signing.loads(request.POST.get('selection',''),salt=SALT,max_age=900)
                if token['actor']!=actor.pk or token['org']!=org.pk or token['deal']!=deal.pk: raise PermissionDenied
                refs={key:token[key] for key in REFS}
                snapshot=reader.read_document(actor=actor,organization_id=org.pk,deal_id=deal.pk,**refs)
                actor,org,deal=reader.access(actor,org.pk,deal.pk)
                if not onec_diagnostic.can_access_diagnostic_mcp(actor,org): raise PermissionDenied
                if snapshot.snapshot_hash!=token['hash']:
                    observe_document(actor=actor,organization_id=org.pk,deal_id=deal.pk,snapshot=snapshot)
                    context['error']='Источник изменился (source_changed). Проверьте новое распределение; прежнее требует сверки.'
                    status=409
                else:
                    columns=[request.POST.getlist(key) for key in ('line','month','quantity','amount')]
                    if len({len(col) for col in columns})!=1 or not 1<=len(columns[0])<=200: raise ValidationError('Выберите строки и периоды.')
                    lines={line.number:line for line in snapshot.lines};items=[]
                    for number,month,quantity,amount in zip(*columns):
                        n=int(number);line=lines[n]
                        items.append(Allocation(n,line.content_hash,date.fromisoformat(month+'-01'),Decimal(quantity),Decimal(amount)))
                    result=save_allocations(actor=actor,organization_id=org.pk,deal_id=deal.pk,snapshot=snapshot,
                        allocations=items,operation_key=token['operation'],expected_revision=token['revision'],expected_snapshot_hash=token['hash'],
                        confirm_contract=request.POST.get('contract_confirm')=='yes',confirm_service=request.POST.get('service_confirm')=='yes',
                        reconcile=request.POST.get('reconcile')=='yes',reason=request.POST.get('reason',''))
                    if result.get('replayed') and not result.get('active'):
                        context['error']='Эта операция уже выполнена, но её версия заменена. Проверьте текущую историю и подтвердите сверку.';status=409
                    elif not result.get('saved'):
                        context['error']='requires_reconciliation: связь изменилась. Проверьте документ и подтвердите сверку.';status=409
                    else:
                        return redirect(request.path+'?org_id='+str(org.pk)+'&saved=1')
            else: raise ValidationError('Неизвестное действие.')
            doc=ServiceBoundDocument.objects.filter(contract__deal=deal,document_ref=snapshot.document_ref).first()
            context['snapshot']=snapshot
            context['selection']=signing.dumps(dict(actor=actor.pk,org=org.pk,deal=deal.pk,**refs,
                hash=snapshot.snapshot_hash,revision=doc.revision if doc else 0,operation='allocate-'+uuid4().hex),salt=SALT)
            context['needs_reconcile']=bool(doc and doc.active_version_id)
            context['source_blocked']=bool(set(snapshot.warnings)&{'correction_requires_review','ambiguous_equal_lines','future_document','deleted_document','unposted_document'})
        except PermissionDenied: raise
        except reader.DocumentScopeChanged:
            context['error']='source_changed / requires_reconciliation: источник изменился либо его область больше не совпадает. Прежняя привязка заблокирована; требуется сверка источника.';status=409
        except onec_diagnostic.OneCDiagnosticError:
            context['error']='Read-only источник 1С недоступен или схема не подтверждена. Ничего не сохранено; проверьте штатное подключение.';status=503
        except (ValidationError,signing.BadSignature,KeyError,ValueError,TypeError,InvalidOperation):
            context['error']='Сохранение отклонено. Обновите документ и проверьте выбор, версию, периоды и суммы.';status=409
    for doc in docs:
        doc.history=list(doc.versions.order_by('-revision')[:20])
        doc.recent_events=list(doc.servicebindingevent_set.order_by('-pk')[:10])
    return render(request,'pool_service/crm_live/service_bindings.html',context,status=status)
