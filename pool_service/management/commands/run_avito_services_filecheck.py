"""Fixed Services stored-file proof; inventory is the default and read-only."""
import json

from django.core.management.base import BaseCommand, CommandError
from pool_service.communication_models import ChannelConnection
from pool_service.avito_services_media_livecheck import run_stored_file_check


class Command(BaseCommand):
    help = "Inspect scoped approved local photos, or run one explicitly activated file-input check."

    def add_arguments(self, parser):
        parser.add_argument('--connection-id', required=True, type=int)
        parser.add_argument('--account-id', required=True)
        parser.add_argument('--perform', action='store_true')
        parser.add_argument('--expected-sha')

    def handle(self, *args, **options):
        if options['connection_id'] != 4 or options['account_id'] != '446312402':
            raise CommandError('file_check_scope_invalid')
        connection = ChannelConnection.objects.select_related('channel').filter(
            pk=4, external_id='446312402').first()
        if connection is None:
            raise CommandError('file_check_scope_invalid')
        result = run_stored_file_check(connection, perform=options['perform'],
                                      expected_sha=options['expected_sha'])
        self.stdout.write(json.dumps(result, sort_keys=True))
