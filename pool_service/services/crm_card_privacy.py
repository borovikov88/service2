"""Classify card origins without using a reader's communication permissions.

A protected source cannot suppress an independent business event. This helper
does not call card/read policies: call authority itself depends on those cards.
"""
from django.db.models import Exists, OuterRef, Q
from pool_service.communication_models import ConversationMessage, PhoneCall
from pool_service.crm_commercial_models import CrmLeadSource, CrmLeadDealLink
from pool_service.staff_call_models import CallWorkContext
from pool_service.staff_call_models import CallPrivacyHold
from pool_service.call_processing_models import CallPrivateNumber


def card_origins(queryset, organization, *, kind):
    from .crm_lead_access import _private_call_annotations
    calls = PhoneCall.objects.filter(organization=organization).filter(
        Q(connection__organization=organization)
        | Q(source_kind='uploaded', connection__isnull=True)).filter(
        Q(client__isnull=True) | Q(client__organization=organization))
    annotated = _private_call_annotations(calls)
    nonprivate = annotated.filter(_crm_private=False, _crm_held=False)
    private = annotated.filter(Q(_crm_private=True) | Q(_crm_held=True))
    work = nonprivate.filter(_crm_key_current=True).filter(
        Q(source_kind='uploaded') | ~Q(privacy_phone_key='')).exclude(direction='internal')
    messages = ConversationMessage.objects.filter(conversation__organization=organization)
    sources = CrmLeadSource.objects.filter(organization=organization,
        lead__organization=organization, inbox__organization=organization, inbox__scope_verified=True)
    if kind == 'lead':
        sources = sources.filter(Q(lead_id=OuterRef('pk')) | Q(lead_id=OuterRef('origin_lead_id'))
            | ((Q(lead__duplicate_of_id=OuterRef('pk')) | Q(lead__duplicate_of_id=OuterRef('origin_lead_id')))
               & Q(lead__status='closed', lead__result='duplicate')))
    elif kind == 'deal':
        links = CrmLeadDealLink.objects.filter(organization=organization,
            deal_id=OuterRef(OuterRef('pk')), lead__organization=organization)
        sources = sources.filter(Q(lead_id__in=links.values('lead_id'))
            | Q(lead_id__in=links.values('lead__origin_lead_id'))
            | ((Q(lead__duplicate_of_id__in=links.values('lead_id'))
                | Q(lead__duplicate_of_id__in=links.values('lead__origin_lead_id')))
               & Q(lead__status='closed', lead__result='duplicate')))
    else:
        raise ValueError('Unknown card kind')
    sources = sources.alias(
        _private=Exists(private.filter(pk=OuterRef('inbox__source_id'))),
        _work=Exists(work.filter(pk=OuterRef('inbox__source_id'))),
        _message=Exists(messages.filter(pk=OuterRef('inbox__source_id'))))
    private_sources = sources.filter(inbox__source_type='call', _private=True)
    work_sources = sources.filter(Q(inbox__source_type='call', _work=True)
        | Q(inbox__source_type='message', _message=True))
    context_scope = Q(**{kind + '_id': OuterRef('pk')})
    if kind == 'lead':
        context_scope |= Q(lead_id=OuterRef('origin_lead_id')) | (
            (Q(lead__duplicate_of_id=OuterRef('pk')) | Q(lead__duplicate_of_id=OuterRef('origin_lead_id')))
            & Q(lead__status='closed', lead__result='duplicate'))
    contexts = CallWorkContext.objects.filter(context_scope, organization=organization,
        call__organization=organization).alias(
        _private=Exists(private.filter(pk=OuterRef('call_id'))),
        _work=Exists(work.filter(pk=OuterRef('call_id'))))
    # A converted card also retains explicit call contexts from its source lead.
    if kind == 'deal':
        linked_contexts = CallWorkContext.objects.filter(
            Q(lead_id__in=links.values('lead_id')) | Q(lead_id__in=links.values('lead__origin_lead_id'))
            | ((Q(lead__duplicate_of_id__in=links.values('lead_id'))
                | Q(lead__duplicate_of_id__in=links.values('lead__origin_lead_id')))
               & Q(lead__status='closed', lead__result='duplicate')),
            organization=organization, call__organization=organization).alias(
            _private=Exists(private.filter(pk=OuterRef('call_id'))),
            _work=Exists(work.filter(pk=OuterRef('call_id'))))
    else:
        linked_contexts = CallWorkContext.objects.none()
    client_scope = Q(client_id=OuterRef('client_id')) | Q(client_id=OuterRef('contact_id'))
    history = work.filter(client__isnull=False).filter(client_scope)
    message_history = messages.filter(conversation__client__isnull=False).filter(
        Q(conversation__client_id=OuterRef('client_id')) | Q(conversation__client_id=OuterRef('contact_id')))
    return queryset.alias(
        _origin_private_source=Exists(private_sources),
        _origin_private_context=Exists(contexts.filter(_private=True)),
        _origin_private_link=Exists(linked_contexts.filter(_private=True)) if kind == 'deal' else Exists(linked_contexts),
        _origin_work_source=Exists(work_sources),
        _origin_work_context=Exists(contexts.filter(_work=True)),
        _origin_work_link=Exists(linked_contexts.filter(_work=True)) if kind == 'deal' else Exists(linked_contexts),
        _origin_work_history=Exists(history), _origin_message_history=Exists(message_history))


def exclude_private_only_cards(queryset, organization, *, kind):
    restricted_origin = (
        (Q(_origin_private_source=True) | Q(_origin_private_context=True) | Q(_origin_private_link=True))
        & Q(_origin_work_source=False, _origin_work_context=False, _origin_work_link=False,
            _origin_work_history=False, _origin_message_history=False))
    # Keep this shortcut in the SAME database statement. A cached Python test
    # for "no private numbers" could become stale while a query is still lazy.
    # With no protection in this organization, provenance cannot be private.
    return card_origins(queryset, organization, kind=kind).alias(
        _org_private_numbers=Exists(CallPrivateNumber.objects.filter(organization=organization)),
        _org_private_holds=Exists(CallPrivacyHold.objects.filter(
            organization=organization, released_at__isnull=True))).filter(
        Q(_org_private_numbers=False, _org_private_holds=False) | ~restricted_origin)
