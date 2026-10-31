"""Explicit proposal lifecycle. No sending, execution, advance or accounting writes."""
import hashlib
import json
from fractions import Fraction
from decimal import Decimal, InvalidOperation
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from pool_service.crm_commercial_models import CANONICAL_KEY, CrmAccess, CrmCommand, CrmEvent
from pool_service.crm_quote_models import CrmQuote, CrmQuoteEvent
from pool_service.estimate_models import PoolEstimateVersion
from .crm_commercial_access import deals_for_user
from .crm_commercial_commands import _locked_organization, _authorize, _owned_deal, CommercialConflict
from .pool_estimates import allowed_organizations, VARIANTS
from .permissions import company_has_access
from . import estimate_documents

ACTIVE = {'prepared', 'sent', 'sending_unknown'}


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


def scope(deal):
    return {key:getattr(deal,key) for key in ('client_id','object_kind','pool_id','client_object_id','planned_object_name','planned_object_address')}


def read_deal(actor, organization, deal_id):
    if organization.pk not in {org.pk for org in allowed_organizations(actor)}:
        raise PermissionDenied('Требуется существующий доступ к калькулятору этой организации.')
    deal = deals_for_user(actor, organization).select_related('client','pipeline').filter(pk=deal_id).first()
    if not deal: raise PermissionDenied('Сделка недоступна.')
    from pool_service.client_queries import readable_clients
    if deal.client_id and not readable_clients(actor).filter(pk=deal.client_id,organization=organization).exists():
        raise PermissionDenied('Клиент недоступен.')
    return deal


def source(quote, deal):
    if quote.scope != scope(deal):
        raise CommercialConflict('Клиент или объект изменился. Создайте новое предложение для текущего контекста; прежняя история сохранена.')
    if quote.version.estimate.organization_id != deal.organization_id or digest(quote.version.snapshot) != quote.source_hash:
        raise CommercialConflict('Сохранённый источник изменён. Требуется сверка, прежнее согласование не переносится.')
    return quote.version.snapshot


def selected_rows(snapshot, variant):
    """Validate frozen values without re-running a newer pricing engine."""
    try:
        rows=[]
        for block in snapshot['variants'][variant]['blocks']:
            if block.get('excluded'): continue
            for row in block['rows']:
                if row.get('excluded') or row.get('existing'): continue
                values={key:row.get(key) for key in ('name','quantity','price','amount')}
                if type(values['name']) is not str: raise ValueError
                for key in ('quantity','price','amount'):
                    raw=values[key]
                    if raw is None: continue
                    if type(raw) is not str or len(raw)>40: raise ValueError
                    number=Decimal(raw)
                    if not number.is_finite() or number<0 or number>=Decimal('10000000000000000'): raise ValueError
                    if number.as_tuple().exponent < (-2 if key=='amount' else -12) or number.as_tuple().exponent>16: raise ValueError
                rows.append(values)
                if len(rows)>300: raise ValidationError('Этот срез КП поддерживает до 300 включённых строк; связывание большего состава пока недоступно.')
        return rows
    except (KeyError,TypeError,ValueError,AttributeError,InvalidOperation) as exc:
        raise ValidationError('Строки сохранённой версии требуют сверки в калькуляторе.') from exc


def readiness(snapshot, variant):
    try:
        selected_rows(snapshot,variant)
        options=estimate_documents.normalize(snapshot.get('documents'))
        options['variants']=[variant]
        errors=estimate_documents.readiness(snapshot,options)
        value=snapshot['variants'][variant]
        if value.get('missing_prices') != 0 or value.get('missing_quantities') != 0:
            errors.append('Сумма варианта неполна: уточните цены и количества в новой версии расчёта.')
        return errors
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValidationError('Версия расчёта не содержит полного формата КП.') from exc


def _amount(version, variant):
    try:
        raw=version.snapshot['variants'][variant]['total']
        if type(raw) is not str or len(raw)>40: raise ValueError
        amount=Decimal(raw)
        if not amount.is_finite() or amount<0 or amount>=Decimal('10000000000000000') or amount.as_tuple().exponent < -2: raise ValueError
        rows=selected_rows(version.snapshot,variant)
        if sum((Fraction(Decimal(row['amount'])) for row in rows if row['amount'] is not None),Fraction(0)) != Fraction(amount): raise ValueError
        return amount
    except (KeyError,TypeError,ValueError,InvalidOperation) as exc:
        raise ValidationError('В сохранённом варианте нет допустимой итоговой суммы.') from exc


def _event(quote, command, actor, action, before, evidence):
    return CrmQuoteEvent.objects.create(organization=quote.organization,quote=quote,command=command,
        actor=actor,action=action,before=before,after=quote.status,evidence=evidence)


@transaction.atomic
def execute(*, actor, organization_id, deal_id, operation_key, expected_revision, action, data):
    if type(operation_key) is not str or len(operation_key)>100: raise ValidationError('Некорректный ключ операции.')
    CANONICAL_KEY(operation_key)
    if any(type(v) is not int or v<=0 for v in (organization_id,deal_id)) or type(expected_revision) is not int or expected_revision<0:
        raise ValidationError('Нужны организация, сделка и её версия.')
    allowed={
        'prepare': {'version_id','variant','confirm_scope','source_hash'},
        'send': {'quote_id','recipient','channel','occurred_at','evidence','outcome','external_message_id'},
        'accept': {'quote_id','evidence','confirm_amount','replace_quote_id'},
        'reject': {'quote_id','evidence'},
        'replace': {'quote_id','replacement_id','evidence'},
    }
    if action not in allowed or type(data) is not dict or not data.keys()<=allowed[action]:
        raise ValidationError('Некорректный состав команды КП.')
    for key in ('version_id','quote_id','replacement_id','replace_quote_id'):
        if key in data and data[key] is not None and (type(data[key]) is not int or data[key]<=0): raise ValidationError('Некорректный идентификатор.')
    for key in ('evidence','recipient','channel','occurred_at','outcome','external_message_id','variant','source_hash'):
        if key in data and (type(data[key]) is not str or len(data[key])>1000): raise ValidationError('Некорректные сведения о решении.')
    payload=digest(dict(actor=actor.pk,deal=deal_id,revision=expected_revision,action=action,data=data))
    org=_locked_organization('default',organization_id)
    actor=_authorize('default',actor.pk,org,'edit_own_deals')
    if not company_has_access(org): raise PermissionDenied
    read_deal(actor,org,deal_id)
    deal=_owned_deal('default',actor.pk,org.pk,deal_id)
    previous=CrmCommand.objects.select_for_update().filter(organization=org,operation_key=operation_key).first()
    if previous:
        if previous.actor_id!=actor.pk or previous.command_type!='quote.'+action or previous.payload_hash!=payload:
            raise CommercialConflict('Ключ операции уже использован с другим содержимым.')
        quote=CrmQuote.objects.select_related('version__estimate').get(pk=previous.result['quote_id'],deal=deal)
        source(quote,deal)
        return previous.result
    if deal.revision!=expected_revision: raise CommercialConflict('Сделка или КП изменены. Обновите страницу перед подтверждением.')
    if deal.is_archived or deal.outcome!='open': raise ValidationError('Изменения КП доступны в открытой сделке.')
    if not deal.client_id: raise ValidationError('Сначала укажите клиента сделки.')
    if action=='prepare':
        if data.get('confirm_scope') is not True or data.get('variant') not in VARIANTS: raise ValidationError('Явно подтвердите клиента, объект и выбранный вариант.')
        version=PoolEstimateVersion.objects.select_for_update().select_related('estimate').filter(pk=data.get('version_id'),estimate__organization=org).first()
        if not version: raise PermissionDenied('Версия расчёта недоступна.')
        if data.get('source_hash') != digest(version.snapshot): raise CommercialConflict('Версия источника изменилась после просмотра.')
        if CrmQuote.objects.filter(deal=deal,version=version,variant=data['variant']).exists():
            raise CommercialConflict('Этот вариант версии уже связан со сделкой; используйте существующее КП.')
        quote=CrmQuote.objects.create(organization=org,deal=deal,version=version,variant=data['variant'],
            source_hash=digest(version.snapshot),scope=scope(deal),amount=_amount(version,data['variant']),created_by=actor)
        before=''
    else:
        quote=CrmQuote.objects.select_for_update().select_related('version__estimate').filter(pk=data.get('quote_id'),deal=deal).first()
        if not quote: raise PermissionDenied('КП недоступно.')
        snapshot=source(quote,deal)
        before=quote.status
        if before not in ACTIVE: raise CommercialConflict('Решение уже принято. Принятое КП заменяется только явным принятием другого предложения.')
        if not data.get('evidence','').strip(): raise ValidationError('Укажите основание решения.')
        if action in {'send','accept'}:
            errors=readiness(snapshot,quote.variant)
            if errors: raise ValidationError(errors)
    command=CrmCommand.objects.create(organization=org,actor=actor,operation_key=operation_key,
        command_type='quote.'+action,payload_hash=payload)
    if action=='send':
        try: stamp=parse_datetime(data.get('occurred_at',''))
        except ValueError as exc: raise ValidationError('Некорректная дата отправки.') from exc
        if not stamp or timezone.is_naive(stamp) or stamp>timezone.now() or not data.get('recipient','').strip() or not data.get('channel','').strip() or data.get('outcome') not in {'sent','unknown'}:
            raise ValidationError('Нужны адресат, способ, прошедшая дата с часовым поясом и фактический результат отправки.')
        quote.status='sent' if data['outcome']=='sent' else 'sending_unknown'
    elif action=='accept':
        if data.get('confirm_amount') is not True: raise ValidationError('Подтвердите согласованную сумму выбранного варианта.')
        if deal.currency not in {'','RUB'}: raise ValidationError('Историческая валюта сделки не RUB; автоматическая конвертация запрещена.')
        old=CrmQuote.objects.select_for_update().filter(deal=deal,status='accepted').first()
        if (old.pk if old else None)!=data.get('replace_quote_id'):
            raise CommercialConflict('Состав принятого КП изменился. Подтвердите замену актуального предложения.')
        if old:
            old.status='replaced';old.revision+=1;old.save()
            _event(old,command,actor,'replace','accepted',{'replacement_id':quote.pk,'evidence':data['evidence']})
        quote.status='accepted'
        deal.amount,deal.currency,deal.amount_basis=quote.amount,'RUB','agreed'
    elif action=='reject': quote.status='rejected'
    elif action=='replace':
        replacement=CrmQuote.objects.select_for_update().select_related('version__estimate').filter(pk=data.get('replacement_id'),deal=deal,status__in=ACTIVE).exclude(pk=quote.pk).first()
        if not replacement: raise ValidationError('Выберите другое действующее КП этой сделки.')
        source(replacement,deal)
        quote.status='replaced'
    if action!='prepare': quote.revision+=1;quote.save()
    _event(quote,command,actor,action,before,data)
    prior=deal.revision;deal.revision+=1;deal.save()
    CrmEvent.objects.create(organization=org,deal=deal,command=command,actor=actor,event_type='quote.'+action,
        safe_diff={'revision':{'before':prior,'after':deal.revision}})
    result={'quote_id':quote.pk,'deal_id':deal.pk,'revision':deal.revision}
    command.result=result;command.save(update_fields=['result'])
    return result
