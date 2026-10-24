"""Future protected source-pin command; both actual merge markers are mandatory."""
import json
import os
from pathlib import Path
import subprocess

from django.core.management.base import BaseCommand, CommandError
from pool_service.management.commands.run_avito_services_release_operation import verify_checkout
from pool_service.avito_services_source_pin import GENERATION, R1_MARKER, R2_MARKER, run_source_pin

FRAME = 'AVITO_SERVICES_SOURCE_PIN_RECEIPT '


def verify_markers(expected_sha):
    try:
        result = subprocess.run(['git', 'show', '-s', '--format=%B', expected_sha],
            cwd=Path(__file__).resolve().parents[3], capture_output=True,
            text=True, timeout=5, check=True)
        valid = R1_MARKER in result.stdout and R2_MARKER in result.stdout
    except Exception:
        valid = False
    if not valid:
        raise CommandError('source pin merge markers required')


class Command(BaseCommand):
    help = 'Protected unchanged services source publication after verified r1 photo restore.'

    def add_arguments(self, parser):
        parser.add_argument('--expected-sha', required=True)
        parser.add_argument('--release-generation', required=True)

    def handle(self, *args, **options):
        if os.environ.get('SERVICE2_AVITO_RELEASE_OPERATOR') != '1':
            raise CommandError('protected source pin operator required')
        if options['release_generation'] != GENERATION:
            raise CommandError('source pin generation invalid')
        verify_checkout(options['expected_sha'])
        verify_markers(options['expected_sha'])
        try:
            result = run_source_pin(options['expected_sha'], options['release_generation'])
        except Exception:
            raise CommandError('source pin unavailable; raw error withheld') from None
        self.stdout.write(FRAME + json.dumps(result, ensure_ascii=False, allow_nan=False))
