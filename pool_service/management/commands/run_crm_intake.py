"""Bounded recurring delivery for one explicitly enabled organization."""
import json
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pool_service.communication_models import ChannelConnection, TelephonyConnection
from pool_service.crm_commercial_models import CrmIntakeActivation
from pool_service.crm_inbox_models import CrmInboxEvent
from pool_service.models import Organization
from pool_service.services.crm_inbox import reconcile, run_worker
from pool_service.services.crm_lead_intake import consume_incoming
from pool_service.services.permissions import company_has_access


class Command(BaseCommand):
    help = "Process a bounded batch of saved live CRM incoming events. Never sends messages."

    def add_arguments(self, parser):
        parser.add_argument("--organization", type=int, required=True)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--seconds", type=int, default=30)

    def handle(self, *args, **options):
        org_id, limit, seconds = options["organization"], options["limit"], options["seconds"]
        if org_id < 1 or not 1 <= limit <= 500 or not 1 <= seconds <= 60:
            raise CommandError("crm_intake_invalid_bounds")
        org = Organization.objects.filter(pk=org_id, crm_v2_read=True, crm_v2_write=True, crm_intake_process=True).first()
        activation = CrmIntakeActivation.objects.filter(organization_id=org_id).first()
        if not org or not activation or not company_has_access(org):
            self.stdout.write(json.dumps({"status": "disabled", "processed": 0}))
            return
        deadline = time.monotonic() + seconds
        counts, error = {}, ""
        # Process oldest due scopes first, avoiding starvation across channels.
        now = timezone.now()
        due = Q(state__in=("pending", "retry", "awaiting_consumer"), next_attempt_at__lte=now) | Q(state="claimed", lease_until__lte=now)
        rows = CrmInboxEvent.objects.filter(due, organization=org).order_by("pk").values_list(
            "source_type", "connection_id_snapshot")[:limit]
        scopes = list(dict.fromkeys(rows))
        processed = 0
        try:
            for kind, connection_id in scopes:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or processed >= limit:
                    break
                result = run_worker(organization_id=org_id, source_type=kind, connection_id=connection_id,
                    limit=min(25, limit - processed), seconds=min(remaining, 5), consumer=consume_incoming)
                for state, count in result.items():
                    counts[state] = counts.get(state, 0) + count
                    processed += count
            # Repair a bounded missing-capture window from locally saved sources.
            # No provider history/API and no legacy-import mode.
            sources = [("message", pk) for pk in ChannelConnection.objects.filter(
                channel__organization=org, channel__kind__in=("website", "avito"),
                channel__is_active=True, is_active=True).order_by("pk").values_list("pk", flat=True)[:20]]
            sources += [("call", pk) for pk in TelephonyConnection.objects.filter(
                organization=org, is_active=True).order_by("pk").values_list("pk", flat=True)[:20]]
            for kind, connection_id in sources:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                result = reconcile(organization_id=org_id, source_type=kind, connection_id=connection_id,
                    limit=20, seconds=min(remaining, 1), include_legacy=False)
                if result.get("error_code"):
                    error = "reconcile_failed"
            if counts.get("retry") or counts.get("failed"):
                error = "consumer_failed"
        except Exception:
            # Never print source content, provider IDs or exception values.
            error = "worker_failed"
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=org_id)
            activation = CrmIntakeActivation.objects.select_for_update().get(organization_id=org_id)
            activation.last_run_at, activation.last_error = timezone.now(), error
            activation.save(update_fields=["last_run_at", "last_error"])
        self.stdout.write(json.dumps({"status": "error" if error else "complete", "processed": processed,
            "states": counts, "error_code": error}, sort_keys=True))
        if error:
            raise CommandError(error)
