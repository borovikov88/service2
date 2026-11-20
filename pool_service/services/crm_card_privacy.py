"""Classify card origins without using a reader's communication permissions.

A protected source cannot suppress an independent business event. This helper
does not call card/read policies: call authority itself depends on those cards.
"""
from django.db.models import Exists, OuterRef, Q
from pool_service.communication_models import ChannelConnection, ConversationMessage, PhoneCall
from pool_service.crm_commercial_models import CrmCommunicationRoute, CrmLeadSource, CrmLeadDealLink
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
    work = nonprivate.filter(internal_contact_number__isnull=True, _crm_key_current=True).filter(
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
    if kind == 'lead':
        # A linked duplicate does not establish the origin of an independent
        # target card. Its protected events stay hidden in the timeline.
        private_sources = private_sources.filter(
            Q(lead_id=OuterRef('pk')) | Q(lead_id=OuterRef('origin_lead_id')))
    else:
        origin_links = links.exclude(kind='existing')
        private_sources = private_sources.filter(Q(lead_id__in=origin_links.values('lead_id'))
            | Q(lead_id__in=origin_links.values('lead__origin_lead_id')))
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
    private_contexts = contexts.filter(_private=True)
    if kind == 'lead':
        private_contexts = private_contexts.filter(
            Q(lead_id=OuterRef('pk')) | Q(lead_id=OuterRef('origin_lead_id')))
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
        private_linked_contexts = linked_contexts.filter(_private=True).filter(
            Q(lead_id__in=origin_links.values('lead_id'))
            | Q(lead_id__in=origin_links.values('lead__origin_lead_id')))
    else:
        linked_contexts = CallWorkContext.objects.none()
        private_linked_contexts = linked_contexts
    routed_work = CrmCommunicationRoute.objects.none()
    if kind == "deal":
        verified_channel = ChannelConnection.objects.filter(pk=OuterRef("inbox__channel_id_snapshot"),
            external_id=OuterRef("inbox__account_id_snapshot"), channel__kind=OuterRef("inbox__provider_kind"),
            is_active=True, channel__is_active=True, channel__organization=organization)
        routed_work = CrmCommunicationRoute.objects.filter(organization=organization, deal_id=OuterRef("pk"),
            inbox__organization=organization, inbox__scope_verified=True).filter(Exists(verified_channel)).alias(
            _work_call=Exists(work.filter(pk=OuterRef("inbox__source_id"), source_kind=PhoneCall.SOURCE_TELEPHONY,
                direction__in=("in", "out"), connection__is_active=True, intake_received_at__isnull=False,
                connection_id=OuterRef("inbox__connection_id_snapshot"), external_id=OuterRef("inbox__external_id"),
                intake_scope__organization=organization.pk, intake_scope__channel=OuterRef("inbox__channel_id_snapshot"),
                intake_scope__account=OuterRef("inbox__account_id_snapshot")).filter(
                    Q(work_context__isnull=True) | Q(work_context__deal_id=OuterRef("deal_id"),
                        work_context__organization=organization))),
            _work_message=Exists(messages.filter(pk=OuterRef("inbox__source_id"), direction__in=("in", "out"),
                conversation__connection_id=OuterRef("inbox__connection_id_snapshot"),
                conversation__external_id=OuterRef("inbox__namespace"), intake_received_at__isnull=False,
                intake_scope__organization=organization.pk, intake_scope__channel=OuterRef("inbox__channel_id_snapshot"),
                intake_scope__external=OuterRef("inbox__external_id"))))
        routed_work = routed_work.filter(Q(inbox__source_type="call", _work_call=True)
            | Q(inbox__source_type="message", _work_message=True))
    client_scope = Q(client_id=OuterRef('client_id')) | Q(client_id=OuterRef('contact_id'))
    history = work.filter(client__isnull=False).filter(client_scope)
    message_history = messages.filter(conversation__client__isnull=False).filter(
        Q(conversation__client_id=OuterRef('client_id')) | Q(conversation__client_id=OuterRef('contact_id')))
    return queryset.alias(
        _origin_private_source=Exists(private_sources),
        _origin_private_context=Exists(private_contexts),
        _origin_private_link=Exists(private_linked_contexts),
        _origin_work_source=Exists(work_sources),
        _origin_work_context=Exists(contexts.filter(_work=True)),
        _origin_work_route=Exists(routed_work),
        _origin_work_link=Exists(linked_contexts.filter(_work=True)) if kind == 'deal' else Exists(linked_contexts),
        _origin_work_history=Exists(history), _origin_message_history=Exists(message_history))


def exclude_private_only_cards(queryset, organization, *, kind):
    numbers = CallPrivateNumber.objects.filter(organization=organization)
    holds = CallPrivacyHold.objects.filter(organization=organization, released_at__isnull=True)
    protection = {'_org_private_numbers': Exists(numbers), '_org_private_holds': Exists(holds)}
    if not numbers.exists() and not holds.exists():
        # The Python check only selects a cheaper query. Both guards remain in
        # the final SQL: a new rule/hold before lazy evaluation returns no rows.
        return queryset.alias(**protection).filter(
            _org_private_numbers=False, _org_private_holds=False)
    restricted_origin = (
        (Q(_origin_private_source=True) | Q(_origin_private_context=True) | Q(_origin_private_link=True))
        & Q(_origin_work_source=False, _origin_work_context=False, _origin_work_link=False, _origin_work_route=False,
            _origin_work_history=False, _origin_message_history=False))
    # Keep this shortcut in the SAME database statement. A cached Python test
    # for "no private numbers" could become stale while a query is still lazy.
    # With no protection in this organization, provenance cannot be private.
    return card_origins(queryset, organization, kind=kind).alias(**protection).filter(
        Q(_org_private_numbers=False, _org_private_holds=False) | ~restricted_origin)
