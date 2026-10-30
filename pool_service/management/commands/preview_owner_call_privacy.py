"""Read-only setup diagnosis. No provider calls, writes, phones or labels."""
import json
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from pool_service.models import OrganizationAccess
from pool_service.communication_models import TelephonyEmployeeIdentity
from pool_service.call_processing_models import CallPrivateNumber
from pool_service.services.owner_call_privacy import verified_owner


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--organization', type=int, required=True)
        parser.add_argument('--owner', type=int, required=True)

    def handle(self, *args, **options):
        org, owner = options['organization'], options['owner']
        if not OrganizationAccess.objects.filter(organization_id=org, user_id=owner, role='owner', user__is_active=True).exists():
            raise CommandError('active_owner_required')
        rows = list(TelephonyEmployeeIdentity.objects.filter(organization_id=org, employee__user_id=owner).select_related('connection'))
        ready = sum(verified_owner(row.connection, row.external_user, row.extension, timezone.now()) == owner for row in rows)
        self.stdout.write(json.dumps({'owner_identity_rows':len(rows), 'verified_current_rows':ready,
            'private_peer_numbers':CallPrivateNumber.objects.filter(organization_id=org,owner_id=owner).count(),
            'early_gate_ready':ready > 0, 'historical_or_ambiguous_identity':'requires_review',
            'provider_recording_changed':False, 'writes':False}, sort_keys=True))
