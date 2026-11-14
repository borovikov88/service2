from datetime import date
from uuid import uuid4
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import HttpResponseBadRequest
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods
from .crm_live_views import _organization
from .crm_quote_views import _integer
from .crm_construction_models import CrmConstructionLaunch
from .models import CrmItem, ServiceTask
from .services import crm_quotes as quotes, crm_construction as service
from .services.crm_permissions import crm_items_for_user, crm_staff_users
from .services.task_read_permissions import readable_tasks, readable_task_queryset
from .services.crm_commercial_commands import CommercialConflict

SALT='crm-construction-launch-v1'


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def workspace(request,deal_id):
    org=_organization(request);deal=quotes.read_deal(request.user,org,deal_id)
    base=reverse('crm_construction',args=[deal.pk])+'?org_id='+str(org.pk)
    error='';status=200;preview=None
    if request.method=='POST':
        if len(request.POST.urlencode())>32768: return HttpResponseBadRequest('Слишком большой запрос.')
        try:
            if request.POST.get('action')=='preview':
                data=service.preview(actor=request.user,organization_id=org.pk,deal_id=deal.pk,
                    project_id=_integer(request.POST.get('project_id')),task_ids=[_integer(v) for v in request.POST.getlist('task_id')],
                    recipient_id=_integer(request.POST.get('recipient_id')),document_note=request.POST.get('document_note',''),
                    planned_start=date.fromisoformat(request.POST.get('planned_start','')),planned_end=date.fromisoformat(request.POST.get('planned_end','')))
                preview={'data':data,'token':signing.dumps(dict(actor=request.user.pk,org=org.pk,basis=data),salt=SALT),'key':uuid4().hex}
            elif request.POST.get('action')=='launch':
                token=signing.loads(request.POST.get('selection',''),salt=SALT,max_age=1800)
                if token['actor']!=request.user.pk or token['org']!=org.pk or token['basis']['deal']!=deal.pk: raise PermissionDenied
                service.launch(actor=request.user,organization_id=org.pk,deal_id=deal.pk,basis=token['basis'],
                    operation_key=request.POST.get('operation_key',''),evidence=request.POST.get('evidence',''),
                    confirm_first_launch=request.POST.get('confirm')=='on')
                return redirect(base+'&saved=1')
            else: raise ValidationError('Неизвестная команда.')
        except PermissionDenied: raise
        except (ValidationError,ValueError,TypeError,KeyError,signing.BadSignature) as exc:
            status=409 if isinstance(exc,CommercialConflict) else 400
            error='; '.join(exc.messages) if isinstance(exc,ValidationError) else 'Некорректное или устаревшее подтверждение.'
    record=CrmConstructionLaunch.objects.filter(deal=deal).first();details=None;blocked=''
    if record:
        try: details=service.visible_record(request.user,org,record)
        except (PermissionDenied,ValidationError): blocked='Пакет требует сверки или источники недоступны. История сохранена; подробности скрыты.'
    supported=deal.fulfillment_kind=='construction' and deal.object_kind=='pool' and deal.pool_id and deal.client_id
    query=crm_items_for_user(request.user,org).filter(direction='project',client_id=deal.client_id,pool_id=deal.pool_id,is_archived=False).order_by('pk')
    if not supported: query=query.none()
    items=Paginator(query,20).get_page(request.POST.get('page') or request.GET.get('page'))
    selected=None;tasks=[];recipients=[]
    selected_id=request.POST.get('project_id') or request.GET.get('project')
    if selected_id:
        try: selected=query.filter(pk=_integer(selected_id)).first()
        except (ValidationError,ValueError): return HttpResponseBadRequest('Некорректный проект.')
        if not selected: raise PermissionDenied
        task_query=ServiceTask.objects.filter(organization=org,crm_item=selected,client_id=deal.client_id,pool_id=deal.pool_id,is_archived=False,completed_at__isnull=True).exclude(status__in=['done','cancelled']).order_by('pk')
        ids=list(readable_task_queryset(task_query,request.user).values_list('pk',flat=True)[:51])
        tasks=readable_tasks(task_query.filter(pk__in=ids),request.user)
        if len(tasks)>50: tasks=[];error='Более 50 задач: требуется отдельная подготовка состава.'
        for candidate in crm_staff_users(org).order_by('pk')[:100]:
            try: quotes.read_deal(candidate,org,deal.pk)
            except PermissionDenied: continue
            if crm_items_for_user(candidate,org).filter(pk=selected.pk).exists(): recipients.append(candidate)
    return render(request,'pool_service/crm_live/construction.html',dict(organization=org,deal=deal,base=base,
        error=error,record=record,details=details,blocked=blocked,preview=preview,items=items,selected=selected,
        tasks=tasks,recipients=recipients,can_launch=bool(supported and service.has_authority(request.user,org) and not record)),status=status)
