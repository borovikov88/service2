"""Current lead/source authority. CRM access never grants communication access."""
from pool_service.services.crm_role_runtime import resolve_crm_capabilities
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import BooleanField, Case, CharField, Exists, F, OuterRef, Q, Value, When, Window
from django.db.models.functions import Coalesce, Concat, Length, Replace, RowNumber, Substr, Trim
from django.urls import reverse

from pool_service.call_processing_models import CallPrivateNumber
from pool_service.communication_models import ChannelConnection, CommunicationAccess, ConversationMessage, PhoneCall
from pool_service.crm_commercial_models import CrmAccess, CrmLead, CrmLeadSource
from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.client_queries import readable_clients
from pool_service.services.call_privacy import private_call_ids


def can_view_leads(user, organization):
    if not user or not user.is_authenticated or not organization:
        return False
    def current(query):
        return query.select_for_update() if transaction.get_connection().in_atomic_block else query
    org = current(Organization.objects).filter(pk=organization.pk).first()
    if not org or not org.crm_v2_read:
        return False
    actor = current(get_user_model().objects).filter(pk=user.pk, is_active=True).first()
    if not actor:
        return False
    if not current(OrganizationAccess.objects).filter(user_id=user.pk, organization=organization).exists():
        return False
    if current(Employee.objects).filter(user_id=user.pk, organization=organization).filter(
        Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED)
    ).exists():
        return False
    grant = resolve_crm_capabilities(user.pk, organization.pk, lock=transaction.get_connection().in_atomic_block)
    return bool(grant and grant.view_leads)


def _communication_caps(user, organization):
    """Use current locking values during commands, including repeatable-read DBs."""
    def current(query):
        return query.select_for_update() if transaction.get_connection().in_atomic_block else query
    actor = current(get_user_model().objects).filter(pk=user.pk, is_active=True).first()
    access = current(OrganizationAccess.objects).filter(user_id=user.pk, organization=organization).order_by("pk").first()
    names = ("can_view_conversations", "can_view_all_calls", "can_view_own_calls")
    if not actor or not access:
        return dict.fromkeys(names, False)
    if actor.is_superuser:
        return dict.fromkeys(names, True)
    grant = current(CommunicationAccess.objects).filter(user_id=user.pk, organization=organization).first()
    return {name: bool(access.role != "accountant" and grant and getattr(grant, name)) for name in names}


def _with_phone_key(queryset, field="phone_number"):
    """SQL counterpart of normalize_phone for scoped communication queries."""
    raw = F(field)
    # Provider writes canonical values; trim legacy whitespace conservatively.
    for char in ("\r", "\n", "\t", "\v", "\f", "\u00a0"):
        raw = Replace(raw, Value(char), Value(" "))
    raw = Trim(raw)
    digits = raw
    for char in ("+", " ", "(", ")", ".", "-"):
        digits = Replace(digits, Value(char), Value(""))
    return queryset.annotate(_crm_raw=raw, _crm_digits=digits).filter(
        Q(_crm_digits="") | Q(_crm_digits__regex=r"^[0-9]+$")
    ).annotate(
        _crm_length=Length("_crm_digits")
    ).annotate(_crm_phone_key=Case(
        When(_crm_raw__startswith="+", _crm_length=11, _crm_digits__startswith="7", then=Substr("_crm_digits", 2)),
        When(_crm_raw__startswith="+", _crm_length__gte=8, _crm_length__lte=15, then=Concat(Value("+"), F("_crm_digits"))),
        When(Q(_crm_length=11) & (Q(_crm_digits__startswith="7") | Q(_crm_digits__startswith="8")), then=Substr("_crm_digits", 2)),
        When(_crm_length=10, then=F("_crm_digits")),
        When(_crm_length__gte=8, _crm_length__lte=15, then=Concat(Value("+"), F("_crm_digits"))),
        default=Value(""), output_field=CharField(),
    ))


def _private_call_annotations(queryset):
    """Actor-independent, confirmed privacy evidence for external calls."""
    from pool_service.staff_call_models import CallPrivacyHold
    held = CallPrivacyHold.objects.filter(call_id=OuterRef('pk'), released_at__isnull=True)
    from .call_privacy import privacy_time_expression
    queryset = queryset.alias(_crm_held=Exists(held), _crm_privacy_at=privacy_time_expression())
    queryset = queryset.alias(_crm_key_current=Case(
        When(privacy_phone_source=F('phone_number'), then=Value(True)),
        default=Value(False), output_field=BooleanField()))
    hidden = CallPrivateNumber.objects.filter(
        organization_id=OuterRef("organization_id"), phone_key=OuterRef('privacy_phone_key'),
    ).filter(Q(owner_id=OuterRef("employee_id")) | Q(owner_id=OuterRef("peer_employee_id"))
             | Q(owner_id=OuterRef('owner_user_snapshot')))
    released = CallPrivacyHold.objects.filter(call_id=OuterRef(OuterRef('pk')),
        owner_id=OuterRef('owner_id'), released_at__isnull=False)
    hidden = hidden.alias(_released=Exists(released)).filter(Q(removed_at__isnull=True)
        | Q(removed_at__gt=OuterRef('_crm_privacy_at'), _released=False))
    return queryset.alias(_crm_private=Case(
        When(_crm_key_current=True, then=Exists(hidden)), default=Value(False), output_field=BooleanField()))


def _without_private_calls(queryset):
    """Bounded source-read predicate; malformed legacy identities fail closed."""
    return _private_call_annotations(queryset).filter(
        _crm_private=False, _crm_held=False, _crm_key_current=True).filter(
        Q(phone_number__regex=r'^\s*$') | ~Q(privacy_phone_key=''))


def _source_access_annotations(user, organization, *, assignment_subject=None):
    """Annotate authority without discarding invalid or foreign source links."""
    org_id = organization.pk
    capabilities = _communication_caps(user, organization)
    source_links = CrmLeadSource.objects.all()
    channel = ChannelConnection.objects.filter(
        pk=OuterRef("inbox__channel_id_snapshot"), is_active=True,
        channel__organization_id=org_id, channel__is_active=True,
        channel__kind=OuterRef("inbox__provider_kind"),
        external_id=OuterRef("inbox__account_id_snapshot"),
    )
    source_links = source_links.alias(_crm_channel=Exists(channel))
    messages = ConversationMessage.objects.filter(
        pk=OuterRef("inbox__source_id"), direction="in",
        conversation__organization_id=org_id,
        conversation__connection_id=OuterRef("inbox__connection_id_snapshot"),
        conversation__external_id=OuterRef("inbox__namespace"),
        conversation__connection__channel__kind__in=("avito", "website"),
        intake_received_at__isnull=False,
        intake_scope__organization=org_id,
        intake_scope__account=OuterRef("inbox__account_id_snapshot"),
        intake_scope__provider=OuterRef("inbox__provider_kind"),
        intake_scope__connection=OuterRef("inbox__connection_id_snapshot"),
        intake_scope__channel=OuterRef("inbox__channel_id_snapshot"),
        intake_scope__namespace=OuterRef("inbox__namespace"),
        intake_scope__external=OuterRef("inbox__external_id"),
    ).filter(Q(conversation__client__isnull=True) | Q(conversation__client__organization_id=org_id)).annotate(
        _crm_external=Coalesce("external_id", Value(""))
    ).filter(_crm_external=OuterRef("inbox__external_id"))
    valid_messages = messages
    if not capabilities["can_view_conversations"]:
        messages = messages.none()
    calls = PhoneCall.objects.filter(
        pk=OuterRef("inbox__source_id"), organization_id=org_id,
        source_kind=PhoneCall.SOURCE_TELEPHONY, direction__in=('in', 'out', 'internal'),
        connection_id=OuterRef("inbox__connection_id_snapshot"),
        connection__organization_id=org_id, connection__is_active=True,
        connection__external_id=OuterRef("inbox__account_id_snapshot"),
        external_id=OuterRef("inbox__external_id"),
        intake_received_at__isnull=False,
        intake_scope__organization=org_id,
        intake_scope__account=OuterRef("inbox__account_id_snapshot"),
        intake_scope__provider="megafon",
        intake_scope__connection=OuterRef("inbox__connection_id_snapshot"),
        intake_scope__channel=OuterRef("inbox__channel_id_snapshot"),
        intake_scope__namespace="",
        intake_scope__external=OuterRef("inbox__external_id"),
    ).filter(Q(client__isnull=True) | Q(client__organization_id=org_id)).filter(
        Q(employee_profile__isnull=True) | Q(employee_profile__organization_id=org_id)
    )
    valid_calls = calls
    from .call_access import scope_calls
    calls = scope_calls(user, organization, calls.exclude(direction='internal'), assignment_subject=assignment_subject)
    calls = _without_private_calls(calls)
    return source_links.alias(_crm_message=Exists(messages), _crm_call=Exists(calls),
        _crm_message_valid=Exists(valid_messages), _crm_call_valid=Exists(valid_calls))


def _readable_source_scope(organization):
    return Q(
        organization_id=organization.pk, lead__organization_id=organization.pk,
        inbox__organization_id=organization.pk, inbox__scope_verified=True,
        _crm_channel=True,
    ) & (
        Q(inbox__source_type="message", _crm_message=True)
        | Q(inbox__source_type="call", inbox__provider_kind="megafon", _crm_call=True)
    )


def _readable_sources(user, organization, *, assignment_subject=None):
    """Scoped locator queryset, with live source permission and connection binding."""
    return _source_access_annotations(user, organization, assignment_subject=assignment_subject).filter(_readable_source_scope(organization))


def leads_for_user(user, organization, *, assignment_subject=None):
    if not can_view_leads(user, organization):
        return CrmLead.objects.none()
    # Structural corruption still closes the card. A private/unauthorized event
    # alone cannot suppress independent, readable work in a mixed card.
    # Anchor each correlated check to this family's source primary keys first.
    # Joining positive tenant/source predicates directly to the OR of two lead
    # IDs lets some planners start at every inbox row in the organization.
    # The locator subquery still includes malformed links for the denial check.
    source_ids = CrmLeadSource.objects.filter(
        Q(lead_id=OuterRef(OuterRef("pk")))
        | Q(lead_id=OuterRef(OuterRef("origin_lead_id"))))
    sources = _source_access_annotations(user, organization, assignment_subject=assignment_subject).filter(
        pk__in=source_ids.values('pk'))
    valid = Q(organization_id=organization.pk, lead__organization_id=organization.pk,
        inbox__organization_id=organization.pk, inbox__scope_verified=True, _crm_channel=True) & (
        Q(inbox__source_type='call', inbox__provider_kind='megafon', _crm_call_valid=True)
        | Q(inbox__source_type='message', _crm_message_valid=True))
    forbidden = sources.filter(~valid)
    readable = sources.filter(_readable_source_scope(organization))
    from pool_service.staff_call_models import CallWorkContext
    from .call_access import scope_calls
    from .crm_card_privacy import exclude_private_only_cards
    contexts = CallWorkContext.objects.filter(organization=organization,
        lead_id=OuterRef(OuterRef('pk'))).values('call_id')
    activity = PhoneCall.objects.filter(organization=organization).exclude(direction='internal').filter(
        Q(pk__in=contexts) | Q(client_id=OuterRef('client_id'), client__isnull=False)
        | Q(client_id=OuterRef('contact_id'), client__isnull=False))
    activity = _without_private_calls(scope_calls(user, organization, activity, assignment_subject=assignment_subject))
    queryset = CrmLead.objects.filter(organization_id=organization.pk).filter(
        Q(client__isnull=True) | Q(client__organization_id=organization.pk)
    ).filter(
        Q(contact__isnull=True) | Q(contact__organization_id=organization.pk, contact__client_type='private')
    ).filter(
        Q(origin_lead__isnull=True) | Q(origin_lead__organization_id=organization.pk, origin_lead__origin_lead__isnull=True)
    ).alias(_crm_forbidden_source=Exists(forbidden), _crm_any_source=Exists(sources),
        _crm_readable_source=Exists(readable), _crm_readable_activity=Exists(activity)).filter(_crm_forbidden_source=False).filter(
        Q(_crm_any_source=False) | Q(_crm_readable_source=True) | Q(_crm_readable_activity=True))
    return exclude_private_only_cards(queryset, organization, kind='lead')


def can_read_lead(user, organization, lead, *, assignment_subject=None):
    return bool(lead and leads_for_user(user, organization, assignment_subject=assignment_subject).filter(pk=lead.pk).exists())


def source_family(user, organization, lead, *, assignment_subject=None):
    """Only direct, explicitly confirmed contributors with current card access."""
    roots = {lead.pk, lead.origin_lead_id} - {None}
    return leads_for_user(user, organization, assignment_subject=assignment_subject).filter(Q(pk__in=roots) |
        Q(duplicate_of_id__in=roots, status="closed", result="duplicate")).values("pk")


def _identity_batches(queryset, field='pk'):
    """Keyset portions, never a total history cap or a nested privacy query."""
    cursor = 0
    while True:
        ids = list(queryset.filter(**{field+'__gt': cursor}).order_by(field)
                   .values_list(field, flat=True).distinct()[:200])
        if not ids:
            return
        yield ids
        cursor = ids[-1]
        if len(ids) < 200:
            return


def has_readable_family_call(user, organization, lead, call_id, *, assignment_subject=None):
    sources = _readable_sources(user, organization, assignment_subject=assignment_subject).filter(
        inbox__source_type='call', inbox__source_id=call_id)
    for ids in _identity_batches(sources, 'lead_id'):
        if source_family(user, organization, lead, assignment_subject=assignment_subject).filter(pk__in=ids).exists():
            return True
    return False


def source_metadata_many(*, user, organization, leads, limit=1, before=None):
    """Bounded, batched metadata from currently readable sources; no bodies/audio."""
    leads = list(leads)
    if not leads:
        return {}
    readable = leads_for_user(user, organization)
    allowed = set(readable.filter(pk__in=[lead.pk for lead in leads]).values_list("pk", flat=True))
    leads = [lead for lead in leads if lead.pk in allowed]
    source_leads = {pk for lead in leads for pk in (lead.pk, lead.origin_lead_id) if pk}
    limit = max(1, min(int(limit), 50))
    # A source may belong both to its immutable original card and to the
    # confirmed continuation target on the same page. Rank each projection
    # separately so page composition cannot change either card's history.
    projections = [
        (readable.filter(pk__in=source_leads), "lead_id"),
        (readable.filter(duplicate_of_id__in=source_leads, status="closed", result="duplicate"), "lead__duplicate_of_id"),
    ]
    top = {}
    for contributors, family in projections:
        # SQLite <=3.45 cannot parse the expanded phone privacy predicate twice
        # inside an IN subquery plus a window wrapper. Evaluate contributor
        # authority separately in bounded keyset portions; source checks remain.
        for ids in _identity_batches(contributors):
            source_rows = _readable_sources(user, organization).filter(lead_id__in=ids)
            if before:
                stamp, source_id = before
                source_rows = source_rows.filter(Q(inbox__received_at__lt=stamp) | Q(inbox__received_at=stamp, pk__lt=source_id))
            batch = source_rows.select_related("inbox").annotate(_family=F(family),
                _source_rank=Window(expression=RowNumber(), partition_by=[F(family)],
                    order_by=[F("inbox__received_at").desc(), F("pk").desc()]))                .filter(_source_rank__lte=limit).order_by("-inbox__received_at", "-pk")
            for row in batch:
                items = top.setdefault(row._family, {})
                items[row.pk] = row
                top[row._family] = {item.pk:item for item in sorted(items.values(),
                    key=lambda item:(item.inbox.received_at,item.pk), reverse=True)[:limit]}
    rows = [row for items in top.values() for row in items.values()]
    # Recheck contributor authority after split reads. A private call alone
    # does not revoke an independent working message on a mixed card.
    current = set()
    for ids in _identity_batches(CrmLead.objects.filter(pk__in={row.lead_id for row in rows})):
        current.update(leads_for_user(user, organization).filter(pk__in=ids).values_list('pk',flat=True))
    rows = [row for row in rows if row.lead_id in current]
    messages = {row.pk: row for row in ConversationMessage.objects.filter(
        pk__in=[row.inbox.source_id for row in rows if row.inbox.source_type == "message"])
        .select_related("conversation").only("pk", "sender_name", "direction", "conversation_id", "conversation__uuid", "conversation__client_id", "conversation__provider_phone", "conversation__participant_phone")}
    calls = {row.pk: row for row in PhoneCall.objects.filter(organization=organization,
        pk__in=[row.inbox.source_id for row in rows if row.inbox.source_type == "call"])
        .select_related("employee", "employee_profile").only("pk", "organization_id", "client_id", "employee_id", "peer_employee_id", "owner_user_snapshot",
            "provider_user", "provider_extension", "peer_provider_user", "peer_provider_extension", "phone_number", "direction", "result",
            "employee_profile_id", "employee_profile__display_name", "employee_profile__organization_id",
            "employee__username", "employee__first_name", "employee__last_name")}
    employee_ids = set(OrganizationAccess.objects.filter(organization=organization,
        user_id__in=[call.employee_id for call in calls.values()]).values_list("user_id", flat=True))
    private_ids = private_call_ids(calls.values())
    contact_ids = {call.client_id for call in calls.values()} | {msg.conversation.client_id for msg in messages.values()}
    contacts = {item.pk: item for item in readable_clients(user).filter(organization=organization, pk__in=contact_ids)}
    grouped = {}
    for row in rows:
        event = row.inbox
        metadata = {"id": row.pk, "type": event.source_type, "provider": event.provider_kind,
            "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
            "received_at": event.received_at.isoformat(), "historical": event.historical,
            "phone": "", "contact_name": "", "direction": "", "employee": "", "contact": None, "caller": "", "callee": ""}
        if event.source_type == "message":
            message = messages.get(event.source_id)
            if not message:
                continue
            contact = contacts.get(message.conversation.client_id)
            metadata.update(phone=message.conversation.provider_phone or message.conversation.participant_phone or "", url=reverse("communications_conversations") + "?" + urlencode({"conversation": str(message.conversation.uuid), "org_id": organization.pk}),
                label="Сообщение Авито" if event.provider_kind == "avito" else "Обращение с сайта",
                contact_name=message.sender_name or "", direction=message.direction, message_id=message.pk)
        else:
            call = calls.get(event.source_id)
            if not call or call.pk in private_ids:
                continue
            contact = contacts.get(call.client_id)
            metadata.update(contact_name=contact.name if contact else "", url=reverse("communications_calls") + "?" + urlencode({"call_id": call.pk, "org_id": organization.pk}),
                label=("Пропущенный звонок" if call.result == PhoneCall.RESULT_MISSED else "Входящий звонок") if call.direction == "in" else "Исходящий звонок" if call.direction == "out" else "Звонок",
                result=call.result,
                phone=call.phone_number or "", direction=call.direction, call_id=call.pk,
                employee=(call.employee_profile.display_name if call.employee_profile_id and call.employee_profile.organization_id == organization.pk else
                    (call.employee.get_full_name() or call.employee.username) if call.employee_id in employee_ids else ""))
        if contact:
            metadata["contact"] = {"id": contact.pk, "name": contact.name, "url": reverse("client_detail", args=[contact.pk])}
        if event.source_type == "call":
            peer = " · ".join(value for value in (contact.name if contact else "Контакт не определён", metadata["phone"]) if value)
            staff = metadata["employee"] or "Сотрудник не определён"
            metadata["caller"], metadata["callee"] = (peer, staff) if metadata["direction"] == "in" else (staff, peer)
        grouped.setdefault(row._family, []).append(metadata)
    result = {}
    for lead in leads:
        items = {row["id"]: row for row in grouped.get(lead.pk, []) + grouped.get(lead.origin_lead_id, [])}
        result[lead.pk] = sorted(items.values(), key=lambda row: (row["received_at"], row["id"]), reverse=True)[:limit]
    return result


def source_metadata(*, user, organization, lead, limit=30, before=None):
    if not can_read_lead(user, organization, lead):
        return []
    return source_metadata_many(user=user, organization=organization, leads=[lead], limit=limit, before=before).get(lead.pk, [])
