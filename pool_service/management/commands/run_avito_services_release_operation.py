"""Protected first-release operation; normal web/worker invocation is not allowed."""
import json
import os
from pathlib import Path
import re
import subprocess

from django.core.management.base import BaseCommand, CommandError
from pool_service.avito_services_release_operation import GENERATION, run_release_operation

FRAME = 'AVITO_SERVICES_RELEASE_OPERATION_RECEIPT '


def verify_checkout(expected_sha):
    if not isinstance(expected_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', expected_sha):
        raise CommandError('release expected SHA invalid')
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'],
            cwd=Path(__file__).resolve().parents[3], capture_output=True,
            text=True, timeout=5, check=True)
        valid = result.stdout.strip() == expected_sha
    except Exception:
        valid = False
    if not valid:
        raise CommandError('release checkout SHA mismatch')


class Command(BaseCommand):
    help = 'Protected services-v15 read/photo-draft proof; never publishes a file or calls Avito writes.'

    def add_arguments(self, parser):
        parser.add_argument('--expected-sha', required=True)
        parser.add_argument('--release-generation', required=True)

    def handle(self, *args, **options):
        if os.environ.get('SERVICE2_AVITO_RELEASE_OPERATOR') != '1':
            raise CommandError('protected release operator required')
        if options['release_generation'] != GENERATION:
            raise CommandError('release generation invalid')
        verify_checkout(options['expected_sha'])
        try:
            result = run_release_operation(options['expected_sha'], options['release_generation'])
        except Exception:
            raise CommandError('release operation unavailable; raw error withheld') from None
        self.stdout.write(FRAME + json.dumps(result, ensure_ascii=False, allow_nan=False))
