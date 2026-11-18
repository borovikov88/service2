"""Private read-only operator entry point. No --apply mode exists."""
import json

from django.core.management.base import BaseCommand, CommandError

from pool_service.services.crm_role_preview_snapshot import UNAVAILABLE, preview_member_crm_roles


class Command(BaseCommand):
    requires_system_checks = []
    help = 'Privately preview CRM flags for one existing member; never changes roles or permissions.'

    def add_arguments(self, parser):
        parser.add_argument('--actor', type=int, required=True)
        parser.add_argument('--organization', type=int, required=True)
        parser.add_argument('--target-user', type=int, required=True)
        parser.add_argument('--roles', nargs='+', required=True)

    def handle(self, *args, **options):
        try:
            result = preview_member_crm_roles(actor_id=options['actor'],
                organization_id=options['organization'], target_id=options['target_user'],
                proposed_roles=options['roles'])
        except Exception:
            raise CommandError(UNAVAILABLE) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
