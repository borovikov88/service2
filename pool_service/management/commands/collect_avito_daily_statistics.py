"""Scoped read-only statistics setup/collection, never a provider write."""
import json
import os
import re
import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pool_service.communication_models import ChannelConnection
from pool_service.avito_daily_statistics import ACCOUNT, ERRORS, configure, collect_next, _state, _allowed, _date, collection_cutoff


class Command(BaseCommand):
    help = 'Read one due Services statistics page or inspect its scoped state.'

    def add_arguments(self, parser):
        parser.add_argument('--connection-id', type=int, required=True)
        parser.add_argument('--account-id', required=True)
        parser.add_argument('--action', choices=('status', 'enable', 'disable', 'collect'), default='status')
        parser.add_argument('--expected-sha')

    def handle(self, *args, **options):
        if options['connection_id'] != 4 or options['account_id'] != ACCOUNT:
            raise CommandError('statistics_scope_invalid')
        connection = ChannelConnection.objects.select_related('channel').filter(pk=4, external_id=ACCOUNT).first()
        if connection is None:
            raise CommandError('statistics_scope_invalid')
        if options['action'] != 'status':
            sha = options['expected_sha']
            if (os.environ.get('SERVICE2_AVITO_DAILY_STATISTICS_SETUP') != '1'
                    or not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha)):
                raise CommandError('protected_statistics_operation_required')
            for command in (['git', 'rev-parse', 'HEAD'],
                            ['git', 'ls-remote', '--exit-code', 'origin', 'refs/heads/main']):
                result = subprocess.run(command, cwd=settings.BASE_DIR, check=False, capture_output=True, timeout=20)
                fields = result.stdout.decode(errors='replace').split()
                if result.returncode or not fields or fields[0] != sha:
                    raise CommandError('statistics_main_changed')
            if options['action'] in {'enable', 'disable'}:
                from pool_service.avito_services_workplace_setup import _actor
                configure(connection, _actor(connection), options['action'])
            else:
                collect_next(4)
        connection.refresh_from_db()
        state = _state(connection)
        code = state.get('last_error_code', '')
        code = code if isinstance(code, str) and (code in ERRORS or code == '' or re.fullmatch(r'provider_http_[1-5][0-9]{2}', code)) else 'statistics_unavailable'
        stamp = lambda name: _date(state.get(name)).isoformat() if _date(state.get(name)) else None
        output = {'kind': 'avito_daily_statistics_state', 'connection_id': 4, 'account_id': ACCOUNT,
            'enabled': state.get('enabled') is True, 'binding_valid': _allowed(connection, state),
            'provider_timezone': 'unknown', 'requested_calendar': 'Asia/Barnaul',
            'lag_days': 2, 'collection_cutoff': collection_cutoff().isoformat(),
            'last_checked_at': stamp('last_checked_at'), 'last_success_at': stamp('last_success_at'),
            'next_due_at': stamp('next_due_at'), 'error_code': code}
        self.stdout.write('AVITO_DAILY_STATISTICS_STATE ' + json.dumps(output, separators=(',', ':')))
