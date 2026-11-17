"""Protected, dry-run-by-default first-owner setup; no automatic admin promotion."""
import json
import os
import re
import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pool_service.services.organization_ownership import OwnershipBlocked, establish_first_owner


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument('--organization-id', type=int, required=True)
        parser.add_argument('--user-id', type=int, required=True)
        parser.add_argument('--expected-sha', required=True)
        parser.add_argument('--expected-plan')
        parser.add_argument('--run-id', required=True)
        parser.add_argument('--run-attempt', required=True)
        parser.add_argument('--apply', action='store_true')

    def handle(self, *args, **options):
        if os.environ.get('SERVICE2_OWNER_BOOTSTRAP') != '1':
            raise CommandError('owner_protected_workflow_required')
        expected = options['expected_sha']
        if not re.fullmatch(r'[a-f0-9]{40}', expected):
            raise CommandError('owner_invalid_commit')
        for field, pattern in [('run_id', r'[1-9][0-9]{0,19}'), ('run_attempt', r'[1-9][0-9]{0,5}')]:
            if not re.fullmatch(pattern, options[field]):
                raise CommandError('owner_invalid_execution')
        if options['apply'] and not re.fullmatch(r'[a-f0-9]{64}', options['expected_plan'] or ''):
            raise CommandError('owner_reviewed_plan_required')
        try:
            actual = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=settings.BASE_DIR,
                check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise CommandError('owner_commit_unavailable') from None
        if actual != expected:
            raise CommandError('owner_commit_mismatch')
        try:
            result = establish_first_owner(organization_id=options['organization_id'],
                user_id=options['user_id'], expected_sha=expected,
                run_id=options['run_id'], run_attempt=options['run_attempt'],
                apply=options['apply'], expected_plan=options['expected_plan'])
        except OwnershipBlocked as exc:
            raise CommandError(str(exc)) from None
        except Exception:
            raise CommandError('owner_operation_failed') from None
        self.stdout.write('ORG_OWNER_BOOTSTRAP_V1 ' + json.dumps(result, sort_keys=True))
