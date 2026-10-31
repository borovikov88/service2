"""Bounded, explicit service handoff preview and acceptance."""
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
from .crm_handoff_models import CrmHandoff
from .crm_commercial_models import CrmAccess
from .models import CrmItem, ServiceTask
from .services import crm_quotes as quotes, crm_handoffs as service
from .services.crm_permissions import crm_items_for_user, crm_staff_users
from .services.task_read_permissions import readable_tasks, readable_task_queryset
from .services.crm_commercial_commands import CommercialConflict

SALT='crm-handoff-preview-v1'


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def workspace(request,deal_id):
    org=_organization(request);deal=quotes.read_deal(request.user,org,deal_id)
    base=reverse('crm_handoff',args=[deal.pk])+'?org_id='+str(org.pk)
    error='';status=200;preview=None
    if request.method=='POST':
        if len(request.POST.urlencode())>32768: return HttpResponseBadRequest('Слишком большой запрос.')
        try:
            action=request.POST.get('action')
            if action=='preview':
                data=service.preview(actor=request.user,organization=org,deal_id=deal.pk,
                    item_id=_integer(request.POST.get('item_id')),task_ids=[_integer(v) for v in request.POST.getlist('task_id')],
                    recipient_id=_integer(request.POST.get('recipient_id')),document_note=request.POST.get('document_note',''))
                preview={'data':data,'token':signing.dumps(dict(actor=request.user.pk,org=org.pk,basis=data),salt=SALT),'key':uuid4().hex}
            else:
                basis=None
                if action=='submit':
                    token=signing.loads(request.POST.get('selection',''),salt=SALT,max_age=1800)
                    if token['actor']!=request.user.pk or token['org']!=org.pk or token['basis']['deal']!=deal.pk: raise PermissionDenied
                    if request.POST.get('confirm')!='on': raise ValidationError('Подтвердите состав, сроки, получателя и документы.')
                    basis=token['basis']
                service.execute(actor=request.user,organization_id=org.pk,deal_id=deal.pk,action=action,
                    operation_key=request.POST.get('operation_key',''),basis=basis,
                    expected_revision=int(request.POST.get('revision','-1')),evidence=request.POST.get('evidence',''))
                return redirect(base+'&saved=1')
        except PermissionDenied: raise
        except (ValidationError,ValueError,KeyError,signing.BadSignature) as exc:
            status=409 if isinstance(exc,CommercialConflict) else 400
            error='; '.join(exc.messages) if isinstance(exc,ValidationError) else 'Некорректное или устаревшее подтверждение.'
    packet=CrmHandoff.objects.filter(deal=deal).first();packet_context=None;blocked=''
    if packet:
        try:
            current=service.current_packet(request.user,org,packet)
            packet_context={'item':current[2],'tasks':current[3],'recipient':current[4],
                'rows':quotes.selected_rows(current[1].version.snapshot,current[1].variant),
                'events':packet.events.select_related('actor').order_by('-pk')[:30]}
        except (PermissionDenied,ValidationError):
            blocked='Пакет требует сверки или его источники недоступны. Прежнее решение не подтверждает текущий состав. Подробности скрыты, история сохранена.'
    supported=bool(deal.client_id and deal.pool_id and deal.object_kind=='pool' and deal.fulfillment_kind=='service')
    item_query=crm_items_for_user(request.user,org).filter(client_id=deal.client_id,pool_id=deal.pool_id,
        direction=CrmItem.DIRECTION_SERVICE,is_archived=False).exclude(stage=CrmItem.STAGE_SERVICE_DONE).order_by('pk')
    if not supported: item_query=item_query.none()
    items=Paginator(item_query,20).get_page(request.GET.get('page'))
    selected=None;tasks=[];recipients=[]
    try:
        item_id=request.POST.get('item_id') or request.GET.get('item')
        if item_id:
            selected=item_query.filter(pk=_integer(item_id)).first()
            if not selected: raise PermissionDenied
            query=ServiceTask.objects.filter(organization=org,crm_item=selected,client_id=deal.client_id,pool_id=deal.pool_id,
                is_archived=False,completed_at__isnull=True).exclude(status__in=['done','cancelled']).order_by('pk')
            candidate_ids=list(readable_task_queryset(query,request.user).values_list('pk',flat=True)[:51])
            tasks=readable_tasks(query.filter(pk__in=candidate_ids),request.user)
            if len(tasks)>50:
                tasks=[];error='В заявке больше 50 открытых задач. Этот ограниченный срез не поддерживает такой пакет.'
            user_ids=set()
            for task in tasks: user_ids.update(task.responsibles.values_list('pk',flat=True))
            for candidate in crm_staff_users(org).filter(pk__in=user_ids).order_by('pk')[:100]:
                try: quotes.read_deal(candidate,org,deal.pk)
                except PermissionDenied: continue
                recipients.append(candidate)
    except (ValidationError,ValueError): error='Некорректный номер заявки.';status=400
    return render(request,'pool_service/crm_live/handoff.html',dict(organization=org,deal=deal,base=base,error=error,
        packet=packet,packet_context=packet_context,blocked=blocked,preview=preview,items=items,selected=selected,
        tasks=tasks,recipients=recipients,operation_key=uuid4().hex,
        can_submit=supported and org.crm_v2_write and deal.manager_id==request.user.pk and not packet
            and CrmAccess.objects.filter(organization=org,user=request.user,edit_own_deals=True).exists(),
        can_accept=packet and packet.status=='pending' and packet.recipient_id==request.user.pk and not blocked),status=status)
