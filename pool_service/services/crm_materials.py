"""Explicit operational evidence. All quantities remain in their original unit.

Lock order: organization -> current account/grants -> issue -> movements -> objects.
No 1C writes, stock balances, payments, auto contracts/tasks or notifications.
"""
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib,json,re
from zoneinfo import ZoneInfo
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied,ValidationError
from django.db import transaction
from django.db.models import Q,Sum,F,Exists,OuterRef
from django.utils import timezone
from pool_service.models import Organization,OrganizationAccess,Employee
from pool_service.crm_material_models import CrmMaterialAccess,CrmMaterialIssue,CrmMaterialMovement,_write_scope
from pool_service.services.crm_deal_objects import object_querysets
from pool_service.services.crm_commercial_access import deals_for_user
from pool_service.services.crm_commercial_commands import CommercialConflict


def today():return timezone.now().astimezone(ZoneInfo('Asia/Barnaul')).date()

def text(value,limit,label):
    value=str(value or '').strip()
    if not value or len(value)>limit:raise ValidationError('Проверьте поле «'+label+'».')
    return value

def key(value):
    value=str(value or '')
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,64}',value):raise ValidationError('Некорректный ключ операции.')
    return value

def qty(value):
    try:
        result=Decimal(str(value).replace(',','.'))
        if not result.is_finite() or result<=0 or result>=Decimal('100000000000') or result!=result.quantize(Decimal('.001')):raise ValueError
        return result.quantize(Decimal('.001'))
    except (InvalidOperation,ValueError,TypeError):raise ValidationError('Количество должно быть положительным, не более трёх знаков после запятой.')

def day(value):
    try:result=value if type(value) is date else date.fromisoformat(str(value))
    except (ValueError,TypeError):raise ValidationError('Укажите фактическую дату.')
    if result>today():raise ValidationError('Будущая дата не подтверждает факт.')
    return result

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()


def authority(actor,organization_id,write=False):
    if not actor or not actor.is_authenticated:raise PermissionDenied
    org=Organization.objects.select_for_update().filter(pk=organization_id).first()
    user=get_user_model().objects.select_for_update().filter(pk=actor.pk,is_active=True).first()
    if not org or not user or not org.crm_v2_read or not org.crm_materials or (write and not org.crm_v2_write):raise PermissionDenied
    roles=set(OrganizationAccess.objects.select_for_update().filter(organization=org,user=user).values_list('role',flat=True))
    if not roles.intersection({'owner','admin','manager','service','installer'}):raise PermissionDenied
    staff=list(Employee.objects.select_for_update().filter(organization=org,user=user))
    if len(staff)!=1 or not staff[0].is_active or staff[0].employment_status==Employee.STATUS_DISMISSED:raise PermissionDenied
    grant=CrmMaterialAccess.objects.select_for_update().filter(organization=org,user=user).first()
    if not grant or not(grant.record_own or grant.review_team):raise PermissionDenied
    return org,user,grant


def target(actor,org,pool_id,deal_id=None):
    pools,_=object_querysets(actor,org.pk,lock=True)
    pool=pools.select_for_update().filter(pk=pool_id,client__organization=org).first()
    if not pool:raise PermissionDenied
    deal=None
    if deal_id:
        deal=deals_for_user(actor,org).select_for_update().filter(pk=deal_id,pool=pool,client_id=pool.client_id,is_archived=False).first()
        if not deal:raise PermissionDenied
    return pool,deal


def visible(actor,org,grant):
    pools,_=object_querysets(actor,org.pk)
    query=CrmMaterialIssue.objects.filter(organization=org,pool__in=pools,client_id=F('pool__client_id'))
    return query if grant.review_team else query.filter(holder=actor)


def balances(issue):
    values=issue.movements.filter(reversal__isnull=True,kind__in=['delivery','return']).values('kind').annotate(total=Sum('quantity'))
    totals={r['kind']:r['total'] for r in values}
    delivered=totals.get('delivery',Decimal(0));returned=totals.get('return',Decimal(0))
    cancelled=issue.movements.filter(kind='void').exists()
    return dict(issued=issue.quantity,delivered=delivered,returned=returned,held=Decimal(0) if cancelled else issue.quantity-delivered-returned,cancelled=cancelled)


@transaction.atomic
@_write_scope()
def issue(*,actor,organization_id,operation_key,pool_id,item_name,unit,packaging,quantity,occurred_on,source,deal_id=None):
    org,user,grant=authority(actor,organization_id,True)
    if not grant.record_own:raise PermissionDenied
    operation_key=key(operation_key);quantity=qty(quantity);occurred_on=day(occurred_on)
    pool,deal=target(user,org,pool_id,deal_id)
    values=dict(pool_id=pool.pk,deal_id=deal.pk if deal else None,item_name=text(item_name,240,'Наименование'),unit=text(unit,40,'Единица'),packaging=text(packaging,120,'Фасовка'),quantity=quantity,occurred_on=occurred_on,source=text(source,500,'Источник'))
    hashed=digest(values)
    old=CrmMaterialIssue.objects.filter(organization=org,created_by=user,operation_key=operation_key).first()
    if old:
        if old.payload_hash!=hashed:raise CommercialConflict('Повтор содержит другие данные.')
        return old
    return CrmMaterialIssue.objects.create(organization=org,client_id=pool.client_id,holder=user,created_by=user,operation_key=operation_key,payload_hash=hashed,**values)


@transaction.atomic
@_write_scope()
def move(*,actor,organization_id,issue_id,operation_key,expected_revision,kind,quantity,occurred_on,source,reason='',pool_id=None,deal_id=None,reverses_id=None):
    org,user,grant=authority(actor,organization_id,True)
    row=visible(user,org,grant).select_for_update().filter(pk=issue_id).first()
    if not row:raise PermissionDenied
    if not grant.review_team and (not grant.record_own or row.holder_id!=user.pk):raise PermissionDenied
    if kind not in {'delivery','return','correction','void'}:raise ValidationError('Операция пока не поддерживается.')
    operation_key=key(operation_key);quantity=qty(quantity);occurred_on=day(occurred_on);source=text(source,500,'Источник')
    reason=str(reason or '').strip()
    if len(reason)>500:raise ValidationError('Слишком длинная причина.')
    if occurred_on<row.occurred_on:raise ValidationError('Движение не может предшествовать выдаче.')
    original=None
    if kind=='void':
        if not grant.review_team:raise PermissionDenied
        if reverses_id or quantity!=row.quantity:raise ValidationError('Отмена выдачи должна охватывать всю ошибочную запись.')
        pool_id=row.pool_id;deal_id=row.deal_id
    elif kind=='correction':
        if not grant.review_team:raise PermissionDenied
        original=row.movements.select_for_update().filter(organization=org,pk=reverses_id,kind__in=['delivery','return']).first()
        if not original or original.quantity!=quantity:raise ValidationError('Отмена должна точно ссылаться на исходную запись.')
        if (original.pool.client_id!=original.client_id or original.pool.organization_id!=org.pk):raise CommercialConflict('Историческая область движения требует сверки.')
        if occurred_on<original.occurred_on:raise ValidationError('Исправление не может предшествовать отменяемому движению.')
        pool_id=original.pool_id;deal_id=original.deal_id
    elif reverses_id:raise ValidationError('Лишнее основание корректировки.')
    elif kind=='return':
        if pool_id and int(pool_id)!=row.pool_id or deal_id:raise ValidationError('Возврат относится к остатку у сотрудника, а не к другой поставке.')
        pool_id=row.pool_id
    if kind=='delivery' and not deal_id and (not pool_id or int(pool_id)==row.pool_id):deal_id=row.deal_id
    pool,deal=target(user,org,pool_id or row.pool_id,deal_id)
    if (kind in {'correction','void'} or pool.pk!=row.pool_id or (kind=='delivery' and (deal.pk if deal else None)!=row.deal_id)) and not reason:raise ValidationError('Для корректировки или другого объекта нужна причина.')
    values=dict(issue_id=row.pk,kind=kind,quantity=quantity,occurred_on=occurred_on,source=source,reason=reason,pool_id=pool.pk,client_id=pool.client_id,deal_id=deal.pk if deal else None,reverses_id=original.pk if original else None)
    hashed=digest(dict(values,expected_revision=expected_revision))
    old=CrmMaterialMovement.objects.filter(organization=org,created_by=user,operation_key=operation_key).first()
    if old:
        if old.payload_hash!=hashed:raise CommercialConflict('Повтор содержит другие данные.')
        return old
    if type(expected_revision) is not int or row.revision!=expected_revision:raise CommercialConflict('Запись уже изменена. Обновите её.')
    if row.movements.exclude(organization=org,pool__in=object_querysets(user,org.pk)[0],client_id=F('pool__client_id')).exists():raise CommercialConflict('Исторические связи требуют отдельной сверки; новые движения недоступны.')
    current=balances(row)
    if current['cancelled']:raise CommercialConflict('Выдача отменена как ошибочная.')
    if kind=='void' and (current['delivered'] or current['returned']):raise ValidationError('Сначала отдельно разберите подтверждённые движения.')
    if original and CrmMaterialMovement.objects.filter(reverses=original).exists():raise CommercialConflict('Исходная запись уже отменена.')
    if kind not in {'correction','void'} and quantity>current['held']:raise ValidationError('Количество превышает подтверждённый остаток у сотрудника.')
    result=CrmMaterialMovement.objects.create(organization=org,created_by=user,operation_key=operation_key,payload_hash=hashed,**values)
    row.revision+=1;row.save(update_fields=['revision'])
    return result


@transaction.atomic
def read(*,actor,organization_id,issue_id=None,before=None):
    org,user,grant=authority(actor,organization_id)
    rows=visible(user,org,grant).select_related('pool','client','holder','created_by').annotate(cancelled=Exists(CrmMaterialMovement.objects.filter(issue_id=OuterRef('pk'),kind='void')))
    if issue_id:
        row=rows.filter(pk=issue_id).first()
        if not row:raise PermissionDenied
        # Reassigned target details need their own current read authorization.
        pools,_=object_querysets(user,org.pk)
        history=row.movements.filter(organization=org,pool__in=pools,client_id=F('pool__client_id')).select_related('pool','created_by','reversal').order_by('-pk')
        restricted=row.movements.exclude(pk__in=history.values('pk')).exists()
        if before:history=history.filter(pk__lt=before)
        return org,grant,row,balances(row),list(history[:21]),restricted
    if before:rows=rows.filter(pk__lt=before)
    return org,grant,list(rows.order_by('-pk')[:51])
