"""Private operator read: one authorized saved call, no processing or repair."""
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from pool_service.communication_models import ChannelConnection, PhoneCall
from pool_service.communication_services import conversation_capability
from pool_service.crm_commercial_models import CrmIntakeActivation, CrmLeadSource
from pool_service.crm_inbox_models import CrmInboxEvent
from pool_service.models import Organization
from pool_service.services.call_privacy import is_private_call
from pool_service.services.crm_inbox import fingerprint, source_scope
from pool_service.services.crm_intake_status import can_read_serving_runtime
from pool_service.services.crm_intake_wakeup import serving_runtime
from pool_service.services.crm_lead_access import can_read_lead
from pool_service.services.permissions import company_has_access


UNAVAILABLE = 'call_diagnostics_unavailable'
LIMIT = 20
STATES = frozenset(('pending', 'claimed', 'awaiting_consumer', 'retry',
    'failed', 'blocked', 'done', 'superseded'))
ERRORS = {
    '': 'none', 'lead_created': 'none', 'lead_linked': 'none',
    'historical_ignored': 'historical', 'legacy_scope_unknown': 'unverified',
    'process_disabled': 'disabled', 'consumer_unavailable': 'consumer_unavailable',
    'consumer_failed': 'processing_failed', 'lease_expired': 'retryable',
    'source_missing': 'source_unavailable', 'source_scope_changed': 'source_unavailable',
}


def _authorized(actor_id, organization_id, call_id):
    # Reload identity and policy; never trust cached roles or superuser flags.
    actor = get_user_model().objects.filter(pk=actor_id, is_active=True).first()
    org = Organization.objects.filter(pk=organization_id).first()
    if not actor or not can_read_serving_runtime(actor, org):
        raise CommandError(UNAVAILABLE)
    calls = PhoneCall.objects.filter(pk=call_id, organization=org, source_kind='telephony')
    if not conversation_capability(actor, 'can_view_all_calls', org):
        if not conversation_capability(actor, 'can_view_own_calls', org):
            raise CommandError(UNAVAILABLE)
        calls = calls.filter(Q(employee_id=actor.pk) | Q(peer_employee_id=actor.pk))
    call = calls.select_related('connection', 'client', 'employee_profile', 'peer_employee_profile').only(
        'id', 'organization_id', 'source_kind', 'direction', 'result', 'duration_seconds',
        'employee_id', 'peer_employee_id', 'phone_number', 'provider_user', 'provider_extension',
        'peer_provider_user', 'peer_provider_extension', 'external_id', 'intake_scope',
        'intake_received_at', 'intake_occurred_at', 'intake_historical', 'intake_revision_historical',
        'connection_id', 'connection__organization_id', 'connection__external_id', 'connection__is_active',
        'client_id', 'client__organization_id', 'employee_profile_id', 'employee_profile__organization_id',
        'peer_employee_profile_id', 'peer_employee_profile__organization_id').first()
    if (not call or is_private_call(call) or not call.connection_id
            or call.connection.organization_id != org.pk or not call.connection.is_active
            or any(row and row.organization_id != org.pk
                   for row in (call.client, call.employee_profile, call.peer_employee_profile))):
        raise CommandError(UNAVAILABLE)
    if call.intake_scope or call.intake_received_at:
        scope = call.intake_scope
        channel = ChannelConnection.objects.filter(
            pk=scope.get('channel') if isinstance(scope, dict) else None,
            channel__organization=org, channel__kind='megafon', channel__is_active=True,
            is_active=True, external_id=call.connection.external_id).first()
        if (not channel or not call.intake_received_at
                or scope != source_scope('call', call, channel, call.connection)):
            raise CommandError(UNAVAILABLE)
    return actor, org, call


def _gate(org, activation, call, *, verified, historical, received, occurred):
    if not (org.crm_v2_read and org.crm_v2_write and org.crm_intake_process and company_has_access(org)):
        return 'disabled'
    if not activation:
        return 'activation_missing'
    if call.direction != 'in':
        return 'not_incoming'
    if not verified:
        return 'unverified'
    if historical or received < activation.starts_at or occurred and occurred < activation.starts_at:
        return 'historical'
    return 'eligible'


class Command(BaseCommand):
    requires_system_checks = []
    help = 'Private authorized operator only: read one call intake chain; never process or repair it.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', type=int, required=True)
        parser.add_argument('--actor', type=int, required=True)
        parser.add_argument('--call-id', type=int, required=True)

    def handle(self, *args, **options):
        try:
            ids = (options['actor'], options['organization'], options['call_id'])
            if any(type(value) is not int or not 0 < value < 2**63 for value in ids):
                raise CommandError(UNAVAILABLE)
            actor, org, call = _authorized(*ids)
            activation = CrmIntakeActivation.objects.filter(organization=org).only('starts_at').first()
            events = list(CrmInboxEvent.objects.filter(organization=org, source_type='call', source_id=call.pk)
                .order_by('-revision').only('id', 'organization_id', 'revision', 'state', 'error_code',
                    'scope_verified', 'historical', 'received_at', 'occurred_at', 'next_attempt_at',
                    'lease_until', 'fingerprint', 'provider_kind', 'connection_id_snapshot',
                    'channel_id_snapshot', 'account_id_snapshot', 'namespace', 'external_id')[:LIMIT + 1])
            links = {link.inbox_id: link for link in CrmLeadSource.objects.filter(
                inbox_id__in=[event.pk for event in events[:LIMIT]]).select_related('lead').only(
                    'organization_id', 'inbox_id', 'lead_id', 'lead__id', 'lead__origin_lead_id')}
            revisions = []
            now = timezone.now()
            for event in events[:LIMIT]:
                if event.state not in STATES:
                    raise CommandError(UNAVAILABLE)
                if event.scope_verified:
                    snapshot = dict(zip(('organization', 'provider', 'connection', 'channel', 'account', 'namespace', 'external'),
                        (event.organization_id, event.provider_kind, event.connection_id_snapshot,
                         event.channel_id_snapshot, event.account_id_snapshot, event.namespace, event.external_id)))
                    if snapshot != call.intake_scope:
                        raise CommandError(UNAVAILABLE)
                link = links.get(event.pk)
                if link and (link.organization_id != org.pk or not can_read_lead(actor, org, link.lead)):
                    raise CommandError(UNAVAILABLE)
                revisions.append({
                    'revision': event.revision, 'state': event.state,
                    'current_facts_match': event.fingerprint == fingerprint('call', call),
                    'historical': event.historical,
                    'activation_gate': _gate(org, activation, call, verified=event.scope_verified,
                        historical=event.historical, received=event.received_at, occurred=event.occurred_at),
                    'due': bool(event.state in ('pending', 'retry', 'awaiting_consumer')
                        and event.next_attempt_at and event.next_attempt_at <= now),
                    'lease_expired': bool(event.state == 'claimed' and event.lease_until and event.lease_until <= now),
                    'lead_linked': link is not None,
                    'error_category': ERRORS.get(event.error_code, 'other'),
                })
            result = {
                'schema_version': 1, 'organization_id': org.pk, 'call_id': call.pk,
                'incoming': call.direction == 'in', 'answered': call.result == 'answered',
                'capture_recorded': bool(events), 'source_receipt_present': call.intake_received_at is not None,
                'source_gate': _gate(org, activation, call, verified=bool(call.intake_scope and call.intake_received_at),
                    historical=call.intake_historical or call.intake_revision_historical,
                    received=call.intake_received_at, occurred=call.intake_occurred_at),
                'readiness': {'crm_read': org.crm_v2_read, 'crm_write': org.crm_v2_write,
                    'process_enabled': org.crm_intake_process, 'company_access': company_has_access(org),
                    'activation_present': activation is not None,
                    'event_wakeup_gate': serving_runtime(org.pk)['configuration_gate'],
                    'event_wakeup_enabled': bool(getattr(settings, 'CRM_INTAKE_EVENT_WAKEUP_ENABLED', False))},
                'revisions': revisions, 'more_revisions': len(events) > LIMIT,
            }
            # Recheck after split reads too; related cards can contain other sources.
            actor, org, _call = _authorized(*ids)
            if any(not can_read_lead(actor, org, link.lead) for link in links.values()):
                raise CommandError(UNAVAILABLE)
        except Exception:
            # The same error/no stdout for absent, foreign, denied, private and corrupt sources.
            raise CommandError(UNAVAILABLE) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
