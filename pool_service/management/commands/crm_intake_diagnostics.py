"""Bounded operator-only metadata; never consumes or changes an inbox event."""
import json
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from pool_service.crm_commercial_models import CrmIntakeActivation
from pool_service.crm_inbox_models import CrmInboxEvent
from pool_service.models import Organization

PREFIX = 'CRM_INTAKE_DIAGNOSTICS_V1 '


class Command(BaseCommand):
    requires_system_checks = []
    help = 'Read capped operational aggregates for pilot organization 1; no CRM mutations.'

    def add_arguments(self, parser):
        parser.add_argument('--organization', type=int, required=True)

    def handle(self, *args, **options):
        if options['organization'] != 1:
            raise CommandError('diagnostics_scope_unavailable')
        try:
            org=Organization.objects.get(pk=1)
            activation=CrmIntakeActivation.objects.filter(organization_id=1).first()
            now=timezone.now()
            buckets={}
            for state in ('pending','retry','awaiting_consumer','claimed','failed','blocked'):
                # Existing (organization,state,next_attempt_at) index; cap materialized
                # rows. Avoid whole-table counts/sorts or source joins/contents.
                rows=list(CrmInboxEvent.objects.filter(organization_id=1,state=state)
                    .order_by('next_attempt_at').values_list('next_attempt_at','lease_until')[:101])
                sample=rows[:100]
                buckets[state]={'sample_count':len(sample),'truncated':len(rows)>100,
                    'due_in_sample':sum(bool(due and due<=now) for due,lease in sample) if state in ('pending','retry','awaiting_consumer') else None,
                    'expired_in_sample':sum(bool(lease and lease<=now) for due,lease in sample) if state=='claimed' else None,
                    'oldest_due_age_seconds':max([int((now-due).total_seconds()) for due,lease in sample if due and due<=now],default=None) if state in ('pending','retry','awaiting_consumer') else None}
            result={'schema_version':1,'organization_id':1,'observed_at':now.isoformat(),
                'crm_v2_read':org.crm_v2_read,'crm_v2_write':org.crm_v2_write,
                'process_enabled':org.crm_intake_process,
                'event_wakeup_enabled':bool(getattr(settings,'CRM_INTAKE_EVENT_WAKEUP_ENABLED',False)),
                'activation_present':activation is not None,
                'cutoff':activation.starts_at.isoformat() if activation else None,
                'last_attempt_at':activation.last_run_at.isoformat() if activation and activation.last_run_at else None,
                'last_attempt_error':bool(activation and activation.last_error),
                'scheduler_registration':'not_observable','scheduler_ticks':'not_recorded',
                'buckets':buckets}
        except Exception:
            raise CommandError('diagnostics_unavailable') from None
        self.stdout.write(PREFIX+json.dumps(result,sort_keys=True))
