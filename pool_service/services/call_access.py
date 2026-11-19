"""Current per-call authority; CRM links never override private visibility."""
from pool_service.services.crm_role_runtime import resolve_crm_capabilities
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Exists, F, OuterRef, Q, Subquery, IntegerField, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from pool_service.communication_models import ChannelConnection, CommunicationAccess, PhoneCall
from pool_service.crm_commercial_models import CrmCommunicationRoute, CrmLead, CrmDeal, CrmLeadSource, CrmLeadDealLink
from pool_service.models import Employee, Organization, OrganizationAccess, DataAuditLog
from pool_service.staff_call_models import CallWorkContext, StaffAccount, CallPrivacyHold


def scope_calls(user, organization, queryset=None, *, media=False, assignment_subject=None):
    """SQL scope used before search, limits, counters and exports; no cached grants."""
    qs = queryset if queryset is not None else PhoneCall.objects.all()
    if not user or not user.is_authenticated or not organization:
        return qs.none()
    # A command may test a proposed assignee before its atomic write. This
    # predicate is never used by read endpoints or to return conversation data.
    if assignment_subject is not None and (
            not transaction.get_connection().in_atomic_block
            or assignment_subject.organization_id != organization.pk
            or not assignment_subject.pk):
        raise ValueError('Assignment preflight requires a locked organization transaction.')
    actor = get_user_model().objects.filter(pk=user.pk, is_active=True).values('is_superuser').first()
    if actor is None or actor['is_superuser']:
        return qs.none()
    roles = set(OrganizationAccess.objects.filter(user_id=user.pk, organization=organization).values_list('role', flat=True))
    if not roles or roles == {'accountant'} or Employee.objects.filter(user_id=user.pk, organization=organization).filter(
            Q(is_active=False) | Q(employment_status='dismissed')).exists():
        return qs.none()
    account = StaffAccount.objects.filter(user_id=user.pk, organization=organization).first()
    if account and account.kind in {'service', 'system'}:
        return qs.none()
    if roles == {'admin'} and not Employee.objects.filter(user_id=user.pk, organization=organization).exists() and not (account and account.kind == 'person'):
        return qs.none()
    grant = CommunicationAccess.objects.filter(user_id=user.pk, organization=organization).first()
    if not grant or not (grant.can_view_all_calls or grant.can_view_own_calls) or media and not grant.can_listen_calls:
        return qs.none()
    qs = qs.filter(organization=organization).filter(
        Q(connection__organization=organization) | Q(source_kind=PhoneCall.SOURCE_UPLOADED, connection__isnull=True)
    ).filter(Q(client__isnull=True) | Q(client__organization=organization)).filter(
        Q(employee_profile__isnull=True) | Q(employee_profile__organization=organization)).filter(
        Q(peer_employee_profile__isnull=True) | Q(peer_employee_profile__organization=organization))
    held = CallPrivacyHold.objects.filter(
        call_id=OuterRef('pk'), released_at__isnull=True)
    qs = qs.alias(_policy_held=Exists(held)).filter(_policy_held=False)
    if 'owner' in roles:
        return qs
    crm = resolve_crm_capabilities(user.pk, organization.pk)
    lead_allowed = bool(crm and crm.view_leads and organization.crm_v2_read)
    # CRM permits staff to read their own deals even without team-deal access.
    from .crm_commercial_access import can_view_team_deals, deal_object_scope
    deal_allowed = can_view_team_deals(user, organization)
    context = CallWorkContext.objects.filter(call_id=OuterRef('pk'))
    sources = CrmLeadSource.objects.filter(organization=organization, lead__organization=organization,
        inbox__organization=organization, inbox__scope_verified=True,
        inbox__source_type='call', inbox__source_id=OuterRef('pk')).order_by()
    count = sources.values('inbox__source_id').annotate(n=Count('lead_id', distinct=True)).values('n')
    qs = qs.annotate(_policy_has_context=Exists(context),
        _policy_context_org=Subquery(context.values('organization_id')[:1]),
        _policy_context_deal=Subquery(context.values('deal_id')[:1]),
        _policy_lead_id=Subquery(context.values('lead_id')[:1]),
        _policy_source_count=Coalesce(Subquery(count, output_field=IntegerField()), Value(0)),
        _policy_source_lead=Subquery(sources.values('lead_id')[:1]))
    qs = qs.annotate(_policy_lead=Coalesce('_policy_lead_id', '_policy_source_lead'))
    links = CrmLeadDealLink.objects.filter(lead_id=OuterRef('_policy_lead')).order_by()
    link_count = links.values('lead_id').annotate(n=Count('deal_id', distinct=True)).values('n')
    qs = qs.annotate(_policy_deals=Coalesce(Subquery(link_count, output_field=IntegerField()), Value(0)))
    lead_assignee = Q(assignee_id=user.pk)
    if isinstance(assignment_subject, CrmLead):
        lead_assignee |= Q(pk=assignment_subject.pk)
    lead = CrmLead.objects.filter(lead_assignee, pk=OuterRef('_policy_lead'), organization=organization,
        is_archived=False)
    deal_assignee = Q(deal__manager_id=user.pk)
    if isinstance(assignment_subject, CrmDeal):
        deal_assignee |= Q(deal_id=assignment_subject.pk)
    converted = links.filter(deal_assignee, organization=organization,
        deal_id__in=deal_object_scope(organization).values('pk'), deal__is_archived=False)
    explicit = context.filter(deal_assignee, organization=organization,
        deal_id__in=deal_object_scope(organization).values('pk'), deal__is_archived=False)
    qs = qs.alias(_policy_assigned_lead=Exists(lead), _policy_assigned_conversion=Exists(converted),
        _policy_assigned_deal=Exists(explicit))
    authority = Q(pk__in=[])
    if lead_allowed:
        authority |= Q(_policy_deals=0, _policy_assigned_lead=True)
    if deal_allowed:
        authority |= Q(_policy_deals=1, _policy_assigned_conversion=True)
        route = CrmCommunicationRoute.objects.filter(deal_assignee, organization=organization,
            deal_id__in=deal_object_scope(organization).values("pk"), deal__is_archived=False,
            inbox__organization=organization, inbox__scope_verified=True, inbox__source_type="call",
            inbox__provider_kind="megafon", inbox__source_id=OuterRef("pk"),
            inbox__connection_id_snapshot=OuterRef("connection_id"), inbox__external_id=OuterRef("external_id"),
            inbox__account_id_snapshot=OuterRef("connection__external_id"),
            inbox__namespace="", inbox__channel_id_snapshot=OuterRef("intake_scope__channel"),
            inbox__account_id_snapshot__exact=OuterRef("intake_scope__account"),
        ).filter(Exists(ChannelConnection.objects.filter(
            pk=OuterRef("inbox__channel_id_snapshot"), external_id=OuterRef("inbox__account_id_snapshot"),
            is_active=True, channel__is_active=True, channel__organization=organization, channel__kind="megafon",
        )))
        qs = qs.alias(_policy_assigned_route=Exists(route))
    explicit_ok = Q(_policy_has_context=True, _policy_context_org=organization.pk) & (
        Q(_policy_context_deal__isnull=False, _policy_assigned_deal=True) if deal_allowed else Q(pk__in=[]))
    if lead_allowed:
        explicit_ok |= Q(_policy_has_context=True, _policy_context_org=organization.pk,
                         _policy_context_deal__isnull=True, _policy_assigned_lead=True)
    derived_ok = Q(_policy_has_context=False, _policy_source_count=1) & authority
    delegated = explicit_ok | derived_ok
    if deal_allowed:
        delegated |= Q(_policy_has_context=False, _policy_source_count=0, _policy_assigned_route=True,
            connection__is_active=True, intake_received_at__isnull=False,
            intake_scope__organization=organization.pk, intake_scope__provider="megafon",
            intake_scope__connection=F("connection_id"), intake_scope__external=F("external_id"),
            intake_scope__namespace="")
    owners = OrganizationAccess.objects.filter(organization=organization, role='owner').values('user_id')
    owner_call = Q(owner_user_snapshot__isnull=False) | Q(employee_id__in=owners) | Q(peer_employee_id__in=owners) | Q(employee_id__isnull=True)
    participants = Q(employee_id=user.pk) | Q(peer_employee_id=user.pk)
    base = Q() if grant.can_view_all_calls else participants
    # Owner conversations require responsibility even when the caller has all-calls.
    internal = Q(direction=PhoneCall.DIRECTION_INTERNAL) & (
        Q(classification_evidence={}) | Q(classification_evidence__kind='internal'))
    return qs.filter((~Q(direction=PhoneCall.DIRECTION_INTERNAL) & ((~owner_call & base) | delegated))
                     | (internal & participants))


def can_read_call(user, call, *, media=False, assignment_subject=None):
    from .call_privacy import is_private_call
    return bool(call and not is_private_call(call) and
                scope_calls(user, call.organization, PhoneCall.objects.filter(pk=call.pk), media=media,
                            assignment_subject=assignment_subject).exists())


@transaction.atomic
def set_work_context(*, actor, call_id, lead_id=None, deal_id=None, revision=0):
    peek = PhoneCall.objects.get(pk=call_id)
    org = Organization.objects.select_for_update().get(pk=peek.organization_id)
    from .call_processing_settings import _lock_owner
    actor = _lock_owner(actor, org)
    from .call_privacy import is_private_call
    call = PhoneCall.objects.select_for_update().get(pk=call_id)
    if not can_read_call(actor, call):
        raise PermissionDenied
    if is_private_call(call) or call.direction == 'internal' or bool(lead_id) == bool(deal_id):
        raise ValidationError('Выберите одну рабочую карточку для внешнего рабочего звонка.')
    from .crm_lead_access import can_read_lead
    from .crm_commercial_access import deals_for_user
    if lead_id:
        subject = CrmLead.objects.filter(organization=org, pk=lead_id).first()
        allowed = can_read_lead(actor, org, subject)
    else:
        allowed = deals_for_user(actor, org).filter(pk=deal_id).exists()
    if not allowed:
        raise PermissionDenied('Выбранная карточка недоступна.')
    row = CallWorkContext.objects.select_for_update().filter(call=call).first()
    if revision != (row.revision if row else 0):
        raise ValidationError('Связь изменилась. Обновите страницу.')
    before = {'lead_id': row.lead_id, 'deal_id': row.deal_id} if row else {}
    row = row or CallWorkContext(call=call, organization=org)
    row.lead_id, row.deal_id = lead_id, deal_id
    row.changed_by, row.changed_at = actor, timezone.now()
    row.revision += 1
    row.full_clean()
    row.save()
    DataAuditLog.objects.create(organization=org, actor=actor, entity_type='CallWorkContext',
        entity_id=str(row.pk), action='update', before=before,
        after={'lead_id': lead_id, 'deal_id': deal_id}, changed_fields=['primary_context'])
    return row


def can_receive_task_notice(user, action_url):
    """Do not send derived call contents through a task notification."""
    from urllib.parse import urlsplit
    from django.urls import resolve, Resolver404
    from pool_service.models import ServiceTask
    from .call_privacy import task_source_is_private
    try:
        match = resolve(urlsplit(action_url or '').path)
    except (ValueError, Resolver404):
        return True
    if 'task_id' not in match.kwargs:
        return True
    task = ServiceTask.objects.filter(pk=match.kwargs['task_id']).first()
    return bool(task and not task_source_is_private(task, user=user))
