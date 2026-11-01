"""Append-only preparation and renewal, never active price/frequency or finance.

No HTTP API yet. A new real contract must use a separately chosen deal, never an
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
               same_contract_confirmed=False):
    """Write a draft version only. Renewal preserves the same draft/deal/object.

    No activate/confirm action is available until operational-source policy is
    decided. Explicit future dates remain proposed obligations, not completed work.
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
        state='draft',operational_effect='none')
    command_hash = digest(dict(actor=actor.pk,deal=deal.pk,action=action,contract_basis=contract_basis.strip(),payer=payer_id,
        proposal=proposed.proposal_hash,reason=reason.strip(),expected_revision=expected_revision,
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
    _,org,deal = _scope(actor,organization_id,deal_id)
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
