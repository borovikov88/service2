"""Read only the bounded, allowlisted last wakeup failure. No database access."""
import json
from django.conf import settings
from django.core.management.base import BaseCommand
from scripts.crm_wakeup_diagnostic import read_failure


class Command(BaseCommand):
    requires_system_checks = []
    help = 'Read the private last wakeup failure; never launches or consumes work.'

    def handle(self, *args, **options):
        self.stdout.write('CRM_WAKEUP_DIAGNOSTIC_V1 ' + json.dumps(
            read_failure(settings.BASE_DIR), sort_keys=True))
