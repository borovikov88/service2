from django.core.management.base import BaseCommand
from pool_service.crm_ui_preview import export_preview


class Command(BaseCommand):
    help = 'Export a standalone synthetic read-only CRM UI; never accesses the database.'
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument('--output', required=True)

    def handle(self, *args, **options):
        self.stdout.write(str(export_preview(options['output'])))
