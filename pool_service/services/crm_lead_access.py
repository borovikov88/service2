"""Current lead/source authority. CRM access never grants communication access."""
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Case, CharField, Exists, F, OuterRef, Q, Value, When
from django.db.models.functions import Coalesce, Concat, Length, Replace, Substr, Trim
from django.urls import reverse

from pool_service.call_processing_models import CallPrivateNumber
from pool_service.communication_models import ChannelConnection, CommunicationAccess, ConversationMessage, PhoneCall
from pool_service.crm_commercial_models import CrmAccess, CrmLead, CrmLeadSource
from pool_service.models import Employee, Organization, OrganizationAccess
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


def source_metadata(*, user, organization, lead, limit=30):
    """Content-free links only; bodies/audio are read in their existing surface."""
    if not can_read_lead(user, organization, lead):
        return []
    rows = list(_readable_sources(user, organization).filter(lead_id__in={lead.pk, lead.origin_lead_id}).select_related("inbox")
        .order_by("-inbox__received_at", "-pk")[:max(1, min(int(limit), 50))])
    message_ids = [row.inbox.source_id for row in rows if row.inbox.source_type == "message"]
    messages = {row.pk: row for row in ConversationMessage.objects.filter(pk__in=message_ids)
        .select_related("conversation").only("pk", "conversation_id", "conversation__uuid")}
    output = []
    for row in rows:
        event = row.inbox
        if event.source_type == "message":
            message = messages.get(event.source_id)
            if not message:
                continue
            url = reverse("communications_conversations") + "?" + urlencode({"conversation": str(message.conversation.uuid), "org_id": organization.pk})
            label = "Сообщение Авито" if event.provider_kind == "avito" else "Обращение с сайта"
        else:
            url = reverse("communications_calls") + "?" + urlencode({"call_id": event.source_id, "org_id": organization.pk})
            label = "Входящий звонок"
        output.append({"id": row.pk, "type": event.source_type, "label": label,
            "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
            "received_at": event.received_at.isoformat(), "historical": event.historical, "url": url})
    return output
