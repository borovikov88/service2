"""Protected owner-authorised Services photo publication; never a bulk Avito action."""
import json
import os
from pathlib import Path
import re
import subprocess

from django.core.management.base import BaseCommand, CommandError
from pool_service.avito_services_approved_photo import (
    GENERATION, MARKER, run_approved_photo_publication)

FRAME = 'AVITO_SERVICES_APPROVED_PHOTO_RECEIPT '


def verify_checkout(expected_sha):
    if not isinstance(expected_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', expected_sha):
        raise CommandError('approved photo SHA invalid')
    try:
        root = Path(__file__).resolve().parents[3]
        head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root,
            capture_output=True, text=True, timeout=5, check=True).stdout.strip()
        message = subprocess.run(['git', 'show', '-s', '--format=%B', 'HEAD'], cwd=root,
            capture_output=True, text=True, timeout=5, check=True).stdout
        clean = subprocess.run(['git', 'diff', '--quiet', 'HEAD', '--', '.github',
            'pool_service', 'service_site', 'manage.py', 'scripts', 'update.sh'], cwd=root,
            capture_output=True, timeout=5).returncode == 0
    except Exception:
        raise CommandError('approved photo checkout unavailable') from None
    if head != expected_sha or MARKER not in message or not clean:
        raise CommandError('approved photo exact marked checkout required')


class Command(BaseCommand):
    help = 'Publish only the approved draft18 photo10 change through the ordinary publisher.'

    def add_arguments(self, parser):
        parser.add_argument('--expected-sha', required=True)
        parser.add_argument('--generation', required=True)

    def handle(self, *args, **options):
        if os.environ.get('SERVICE2_AVITO_APPROVED_PHOTO') != '1':
            raise CommandError('protected approved photo operator required')
        if options['generation'] != GENERATION:
            raise CommandError('approved photo generation invalid')
        verify_checkout(options['expected_sha'])
        try:
            result = run_approved_photo_publication(options['expected_sha'])
        except Exception:
            raise CommandError('approved photo unavailable; raw error withheld') from None
        self.stdout.write(FRAME + json.dumps(result, ensure_ascii=False, allow_nan=False))
