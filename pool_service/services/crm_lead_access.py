"""Current lead/source authority. CRM access never grants communication access."""
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, CharField, Exists, F, OuterRef, Q, Value, When, Window
from django.db.models.functions import Coalesce, Concat, Length, Replace, Substr, Trim, RowNumber
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
    grant = current(CrmAccess.objects).filter(user_id=user.pk, organization=organization).first()
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


def _without_private_calls(queryset):
    """SQL counterpart of phone_key for bounded lists, including national aliases.

    Only external inbound calls reach this query. Direct reads also use the
    canonical Python privacy policy. No historical call list is loaded in Python.
    """
    raw = F("phone_number")
    # Provider writes canonical values; trim legacy whitespace conservatively.
    for char in ("\r", "\n", "\t"):
        raw = Replace(raw, Value(char), Value(" "))
    raw = Trim(raw)
    digits = raw
    for char in ("+", " ", "(", ")", ".", "-"):
        digits = Replace(digits, Value(char), Value(""))
    queryset = queryset.annotate(_crm_raw=raw, _crm_digits=digits).filter(
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
    hidden = CallPrivateNumber.objects.filter(
        organization_id=OuterRef("organization_id"), phone_key=OuterRef("_crm_phone_key"),
    ).filter(Q(owner_id=OuterRef("employee_id")) | Q(owner_id=OuterRef("peer_employee_id")))
    return queryset.annotate(_crm_private=Exists(hidden)).filter(_crm_private=False)


def _source_access_annotations(user, organization):
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
    if not capabilities["can_view_conversations"]:
        messages = messages.none()
    calls = PhoneCall.objects.filter(
        pk=OuterRef("inbox__source_id"), organization_id=org_id,
        source_kind=PhoneCall.SOURCE_TELEPHONY, direction=PhoneCall.DIRECTION_IN,
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
    if not capabilities["can_view_all_calls"]:
        if not capabilities["can_view_own_calls"]:
            calls = calls.none()
        else:
            calls = calls.filter(Q(employee_id=user.pk) | Q(peer_employee_id=user.pk))
    calls = _without_private_calls(calls)
    return source_links.alias(_crm_message=Exists(messages), _crm_call=Exists(calls))


def _readable_source_scope(organization):
    return Q(
        organization_id=organization.pk, lead__organization_id=organization.pk,
        inbox__organization_id=organization.pk, inbox__scope_verified=True,
        _crm_channel=True,
    ) & (
        Q(inbox__source_type="message", _crm_message=True)
        | Q(inbox__source_type="call", inbox__provider_kind="megafon", _crm_call=True)
    )


def _readable_sources(user, organization):
    """Scoped locator queryset, with live source permission and connection binding."""
    return _source_access_annotations(user, organization).filter(_readable_source_scope(organization))


def leads_for_user(user, organization):
    if not can_view_leads(user, organization):
        return CrmLead.objects.none()
    # An inconsistent, revoked or private source hides the derived card. An
    # independent manually created lead has no source link and remains readable.
    # Test the same predicate on each linked source directly. Nesting a second
    # source query through NOT IN overflows older SQLite parsers once the phone
    # privacy expression is expanded. Keep every invalid link in this predicate
    # so corrupt/foreign links still hide the whole card, including its origin.
    forbidden = _source_access_annotations(user, organization).filter(
        Q(lead_id=OuterRef("pk")) | Q(lead_id=OuterRef("origin_lead_id"))
    ).filter(~_readable_source_scope(organization))
    return CrmLead.objects.filter(organization_id=organization.pk).filter(
        Q(client__isnull=True) | Q(client__organization_id=organization.pk)
    ).filter(
        Q(origin_lead__isnull=True) | Q(origin_lead__organization_id=organization.pk, origin_lead__origin_lead__isnull=True)
    ).annotate(_crm_forbidden_source=Exists(forbidden)).filter(_crm_forbidden_source=False)


def can_read_lead(user, organization, lead):
    if not lead or not leads_for_user(user, organization).filter(pk=lead.pk).exists():
        return False
    call_ids = CrmLeadSource.objects.filter(lead_id__in={lead.pk, lead.origin_lead_id}, organization=organization,
        inbox__source_type="call").values_list("inbox__source_id", flat=True).distinct()
    return not private_call_ids(PhoneCall.objects.filter(pk__in=call_ids, organization=organization))


def source_family(user, organization, lead):
    """Only direct, explicitly confirmed contributors with current card access."""
    roots = {lead.pk, lead.origin_lead_id} - {None}
    return leads_for_user(user, organization).filter(Q(pk__in=roots) |
        Q(duplicate_of_id__in=roots, status="closed", result="duplicate")).values("pk")


def source_metadata_many(*, user, organization, leads, limit=1, before=None):
    """Bounded, batched metadata from currently readable sources; no bodies/audio."""
    leads = list(leads)
    if not leads:
        return {}
    allowed = set(leads_for_user(user, organization).filter(pk__in=[lead.pk for lead in leads]).values_list("pk", flat=True))
    leads = [lead for lead in leads if lead.pk in allowed]
    source_leads = {pk for lead in leads for pk in (lead.pk, lead.origin_lead_id) if pk}
    limit = max(1, min(int(limit), 50))
    readable = leads_for_user(user, organization)
    # A source may belong both to its immutable original card and to the
    # confirmed continuation target on the same page. Rank each projection
    # separately so page composition cannot change either card's history.
    projections = [
        (readable.filter(pk__in=source_leads).values("pk"), "lead_id"),
        (readable.filter(duplicate_of_id__in=source_leads, status="closed", result="duplicate").values("pk"), "lead__duplicate_of_id"),
    ]
    rows = []
    for contributors, family in projections:
        source_rows = _readable_sources(user, organization).filter(lead_id__in=contributors)
        if before:
            stamp, source_id = before
            source_rows = source_rows.filter(Q(inbox__received_at__lt=stamp) | Q(inbox__received_at=stamp, pk__lt=source_id))
        rows.extend(source_rows.select_related("inbox").annotate(_family=F(family),
            _source_rank=Window(expression=RowNumber(), partition_by=[F(family)],
                order_by=[F("inbox__received_at").desc(), F("pk").desc()]))
            .filter(_source_rank__lte=limit).order_by("-inbox__received_at", "-pk"))
    messages = {row.pk: row for row in ConversationMessage.objects.filter(
        pk__in=[row.inbox.source_id for row in rows if row.inbox.source_type == "message"])
        .select_related("conversation").only("pk", "sender_name", "direction", "conversation_id", "conversation__uuid", "conversation__client_id", "conversation__provider_phone", "conversation__participant_phone")}
    calls = {row.pk: row for row in PhoneCall.objects.filter(organization=organization,
        pk__in=[row.inbox.source_id for row in rows if row.inbox.source_type == "call"])
        .select_related("employee", "employee_profile").only("pk", "organization_id", "client_id", "employee_id", "peer_employee_id",
            "provider_user", "provider_extension", "peer_provider_user", "peer_provider_extension", "phone_number", "direction",
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
                label="Входящий звонок" if call.direction == "in" else "Исходящий звонок" if call.direction == "out" else "Звонок",
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
