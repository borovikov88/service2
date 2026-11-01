"""Append-only dated terms, explicit confirmation and same-contract renewal.

Confirmation changes read projections only, never finance or stored legacy values. A new real contract must use a separately chosen deal, never an
automatic identity match. Existing 1C bindings are neither created nor reassigned.
"""
from datetime import date
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from pool_service.models import Client
from pool_service.crm_commercial_models import CANONICAL_KEY
from pool_service.crm_recurring_models import CrmRecurringDraft, CrmRecurringRevision
from .crm_commercial_commands import _locked_organization, _authorize, _owned_deal, CommercialConflict
from .permissions import company_has_access
from .crm_recurring_terms import preview_terms
from .crm_quotes import digest


def _object(deal):
    return dict(kind=deal.object_kind,pool=deal.pool_id,client_object=deal.client_object_id)


def _scope(actor, organization_id, deal_id):
    if not actor or not actor.is_authenticated or not actor.is_active:
        raise PermissionDenied
    org = _locked_organization('default', organization_id)
    actor = _authorize('default', actor.pk, org, 'edit_own_deals')
    if not company_has_access(org): raise PermissionDenied
    deal = _owned_deal('default', actor.pk, org.pk, deal_id)
    if (deal.is_archived or deal.outcome != 'open' or deal.fulfillment_kind != 'service'
            or not deal.client_id or deal.object_kind not in ('pool','client_object')):
        raise ValidationError('Нужна открытая сделка обслуживания с клиентом и конкретным объектом.')
    return actor, org, deal


@transaction.atomic
def save_draft(*, actor, organization_id, deal_id, expected_deal_revision, expected_revision,
               operation_key, action, contract_basis, payer_id, terms, reason,
               same_contract_confirmed=False, schedule=None):
    """Write a draft version only. Renewal preserves the same draft/deal/object.

    Confirmation is a separate explicit command. Draft dates remain proposed
    obligations, not completed work.
    """
    if type(operation_key) is not str or not 1 <= len(operation_key) <= 100:
        raise ValidationError('Некорректный ключ операции.')
    CANONICAL_KEY(operation_key)
    if action not in ('prepare','revise','renew'):
        raise ValidationError('Активация условий пока недоступна.')
    for value in (organization_id, deal_id, payer_id):
        if type(value) is not int or value <= 0: raise ValidationError('Нужны явные идентификаторы.')
    if any(type(v) is not int or v < 0 for v in (expected_revision,expected_deal_revision)):
        raise ValidationError('Нужны текущие версии.')
    for value in (contract_basis,reason):
        if type(value) is not str or not 1 <= len(value.strip()) <= 1000:
            raise ValidationError('Укажите договорное основание и причину редакции.')
    if action == 'renew' and same_contract_confirmed is not True:
        raise ValidationError('Явно подтвердите продление того же договора.')
    if type(terms) is not dict or set(terms) != {'start','end','periods','complete_period_list','renewal_review_on'}:
        raise ValidationError('Нужен полный явно заданный проект условий.')
    from pool_service.models import Pool
    if schedule is None: schedule={'frequency':None,'first_visit':None}
    if type(schedule) is not dict or set(schedule)!={'frequency','first_visit'}:
        raise ValidationError('Нужны явные параметры графика.')
    if schedule['frequency'] not in {None, *dict(Pool.SERVICE_FREQUENCY_CHOICES)}:
        raise ValidationError('Неизвестная периодичность.')
    if schedule['frequency'] and type(schedule['first_visit']) is not date:
        raise ValidationError('Укажите первую плановую дату визита, без автоматического назначения.')
    if not schedule['frequency'] and schedule['first_visit'] is not None:
        raise ValidationError('Дата первого визита требует периодичности.')
    if schedule['first_visit'] and (schedule['first_visit']<terms['start'] or (terms['end'] and schedule['first_visit']>terms['end'])):
        raise ValidationError('Первая дата визита вне срока.')
    proposed = preview_terms(**terms)
    actor, org, deal = _scope(actor,organization_id,deal_id)
    from pool_service.client_queries import active_clients, readable_clients
    payer = active_clients(Client.objects.select_for_update()).filter(pk=payer_id,organization=org).first()
    if not payer or not readable_clients(actor).filter(pk=payer.pk,organization=org).exists(): raise PermissionDenied
    scope = _object(deal)
    draft = CrmRecurringDraft.objects.select_for_update().filter(deal=deal).first()
    if draft and (draft.organization_id != org.pk or draft.client_id != deal.client_id or
                  draft.payer_id != payer_id or draft.object_scope != scope or draft.contract_basis != contract_basis.strip()):
        raise CommercialConflict('Клиент, плательщик, объект или договор изменился. Нужна отдельная сверка.')
    payload = dict(start=terms['start'].isoformat(),end=terms['end'].isoformat() if terms['end'] else None,
        complete_period_list=terms['complete_period_list'],renewal_review_on=terms['renewal_review_on'].isoformat(),
        proposal_hash=proposed.proposal_hash,periods=[dict(month=r.month.isoformat(),quantity=str(r.quantity),
            unit=r.unit,agreed_amount=None if r.agreed_amount is None else str(r.agreed_amount)) for r in proposed.periods],
        state='draft',operational_effect='none',schedule=dict(frequency=schedule['frequency'],first_visit=schedule['first_visit'].isoformat() if schedule['first_visit'] else None))
    command_hash = digest(dict(actor=actor.pk,deal=deal.pk,action=action,contract_basis=contract_basis.strip(),payer=payer_id,
        proposal=proposed.proposal_hash,schedule=payload['schedule'],reason=reason.strip(),expected_revision=expected_revision,
        expected_deal_revision=expected_deal_revision,same_contract_confirmed=same_contract_confirmed))
    previous = CrmRecurringRevision.objects.select_for_update().filter(organization=org,operation_key=operation_key).first()
    if previous:
        if not draft or previous.draft_id != draft.pk or previous.payload_hash != command_hash:
            raise CommercialConflict('Ключ уже использован для другой команды.')
        return previous
    if deal.revision != expected_deal_revision: raise CommercialConflict('Сделка изменилась; обновите данные.')
    latest = CrmRecurringRevision.objects.select_for_update().filter(draft=draft).order_by('-revision').first() if draft else None
    if (latest.revision if latest else 0) != expected_revision:
        raise CommercialConflict('Редакция изменилась; обновите данные.')
    if action == 'prepare' and draft: raise CommercialConflict('Проект договора уже существует.')
    if action != 'prepare' and not draft: raise CommercialConflict('Сначала подготовьте существующий договор.')
    if action == 'renew':
        old_end = latest.payload['end']
        if old_end is None or terms['start'] <= date.fromisoformat(old_end):
            raise ValidationError('Продление требует нового срока после прежнего. Бессрочный договор требует отдельного решения.')
        if any(r.month <= date.fromisoformat(old_end).replace(day=1) for r in proposed.periods):
            raise ValidationError('Месяц прежнего срока нельзя повторно включить в продление.')
    if action == 'revise' and (payload['start'],payload['end']) != (latest.payload['start'],latest.payload['end']):
        raise ValidationError('Для нового срока используйте явное продление, прежний срок не переписывается.')
    if draft is None:
        draft = CrmRecurringDraft.objects.create(organization=org,deal=deal,client_id=deal.client_id,payer=payer,
            object_scope=scope,contract_basis=contract_basis.strip(),created_by=actor)
    return CrmRecurringRevision.objects.create(organization=org,draft=draft,revision=expected_revision+1,
        kind=action,payload=payload,operation_key=operation_key,payload_hash=command_hash,actor=actor,reason=reason.strip())


@transaction.atomic
def read_history(*, actor, organization_id, deal_id, before_revision=None):
    """Own-deal history under current write scope; no team access expansion.

    Internal preparation service, bounded 20 versions, no document payload joins.
    """
    actor,org,deal = _scope(actor,organization_id,deal_id)
    draft=CrmRecurringDraft.objects.select_for_update().filter(deal=deal,organization=org).first()
    if not draft: return []
    if draft.client_id != deal.client_id or draft.object_scope != _object(deal): raise PermissionDenied
    from pool_service.client_queries import readable_clients
    if not readable_clients(actor).filter(pk=draft.payer_id,organization=org).exists(): raise PermissionDenied
    rows=CrmRecurringRevision.objects.filter(draft=draft).order_by('-revision')
    if before_revision is not None:
        if type(before_revision) is not int or before_revision <= 0: raise ValidationError('Некорректная страница.')
        rows=rows.filter(revision__lt=before_revision)
    return list(rows[:20])


@transaction.atomic
def confirm(*, actor, organization_id, deal_id, revision_id, expected_revision, effective_from, confirm_effect=False):
    from django.utils import timezone
    from zoneinfo import ZoneInfo
    from pool_service.models import Pool
    from pool_service.crm_recurring_models import CrmRecurringConfirmation
    if confirm_effect is not True or type(effective_from) is not date:
        raise ValidationError('Явно подтвердите применение условий с выбранной даты.')
    actor,org,deal=_scope(actor,organization_id,deal_id)
    draft=CrmRecurringDraft.objects.select_for_update().filter(deal=deal).first()
    if not draft or deal.object_kind!='pool' or draft.client_id!=deal.client_id or draft.object_scope!=_object(deal):
        raise CommercialConflict('Подтверждение поддерживает только неизменный существующий объект Pool.')
    from pool_service.client_queries import readable_clients
    if not readable_clients(actor).filter(pk=draft.payer_id,organization=org).exists():raise PermissionDenied
    pool=Pool.objects.select_for_update().get(pk=deal.pool_id,organization=org,client_id=deal.client_id)
    revision=CrmRecurringRevision.objects.select_for_update().filter(pk=revision_id,draft=draft).first()
    if not revision:raise PermissionDenied
    existing=CrmRecurringConfirmation.objects.select_for_update().filter(revision=revision).first()
    if existing:
        if existing.effective_from!=effective_from or existing.actor_id!=actor.pk:raise CommercialConflict('Подтверждение уже существует.')
        return existing
    latest=CrmRecurringRevision.objects.select_for_update().filter(draft=draft).order_by('-revision').first()
    if latest.pk!=revision.pk or revision.revision!=expected_revision:raise CommercialConflict('Нужна последняя редакция.')
    today=timezone.localdate(timezone.now(),ZoneInfo('Asia/Barnaul'))
    if effective_from<today or effective_from<date.fromisoformat(revision.payload['start']) or (revision.payload['end'] and effective_from>date.fromisoformat(revision.payload['end'])):
        raise ValidationError('Применение только с явно выбранной текущей/будущей даты внутри срока.')
    if 'schedule' not in revision.payload:raise ValidationError('Подготовьте новую редакцию с явным графиком.')
    records=list(CrmRecurringConfirmation.objects.select_for_update().filter(pool=pool).select_related('revision__draft').order_by('effective_from'))
    # One authoritative lineage per object until explicit transfer/reconciliation exists.
    if any(r.revision.draft_id!=draft.pk for r in records):
        raise CommercialConflict('У объекта уже подтверждён другой договор; нужна отдельная сверка, автоматического объединения нет.')
    if records:
        previous=records[-1]
        if effective_from<=previous.effective_from:raise CommercialConflict('Датированные подтверждения должны идти вперёд.')
        old={r['month']:r for r in previous.revision.payload['periods']}
        old_end=previous.revision.payload['end']
        extends_term=bool(old_end and revision.payload['start']>old_end)
        if not extends_term:
            new_rows={r['month']:r for r in revision.payload['periods']}
            for month,row in old.items():
                if month<effective_from.replace(day=1).isoformat() and new_rows.get(month)!=row:
                    raise CommercialConflict('Исторические договорные месяцы нельзя удалять или переписывать.')
        for row in revision.payload['periods']:
            if row['month']<effective_from.replace(day=1).isoformat() and row['month'] in old and row!=old[row['month']]:
                raise CommercialConflict('Исторические договорные месяцы нельзя переписывать.')
        month=effective_from.replace(day=1).isoformat()
        new=next((r for r in revision.payload['periods'] if r['month']==month),None)
        if effective_from.day!=1 and month in old and new!=old[month]:
            raise ValidationError('Изменение месячного объёма/цены внутри месяца требует отдельного распределения.')
    # ServiceVisitPlan supports one obligation per pool/week. Never silently
    # collapse two visits when a revision changes the schedule inside a week.
    if records:
        from datetime import timedelta
        from .crm_recurring_effective import due_dates
        candidate=CrmRecurringConfirmation(organization=org,revision=revision,pool=pool,effective_from=effective_from,actor=actor)
        week=effective_from-timedelta(days=effective_from.weekday())
        dates=due_dates(pool,week,week+timedelta(days=6),records+[candidate])
        if len(dates)>1:
            raise CommercialConflict('В неделе перехода получаются два визита. Выберите границу недели или явно согласуйте график: календарь поддерживает один визит объекта в неделю.')
    return CrmRecurringConfirmation.objects.create(organization=org,revision=revision,pool=pool,effective_from=effective_from,actor=actor)


@transaction.atomic
def _scope_read_ui(actor, organization_id, deal_id):
    return _scope(actor,organization_id,deal_id)
