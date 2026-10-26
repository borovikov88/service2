from django.core.management.base import BaseCommand, CommandError
from pool_service.crm_commercial_bootstrap import bootstrap_commercial_settings
from pool_service.models import Organization


class Command(BaseCommand):
    help = "Create the four proposed CRM pipelines; preserve existing settings and flags."

    def add_arguments(self, parser):
        parser.add_argument("--organization-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            pipelines, stages = bootstrap_commercial_settings(options["organization_id"])
        except Organization.DoesNotExist as exc:
            raise CommandError("Organization does not exist.") from exc
        self.stdout.write(f"Created pipelines={pipelines}, stages={stages}; existing settings preserved.")
