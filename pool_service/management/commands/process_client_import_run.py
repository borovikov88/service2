from django.core.management.base import BaseCommand, CommandError

from pool_service.client_crm_import import process_client_import_run


class Command(BaseCommand):
    help = "Process one queued 1C CRM client import run."

    def add_arguments(self, parser):
        parser.add_argument("run_id")

    def handle(self, *args, **options):
        run_id = str(options["run_id"])
        try:
            run = process_client_import_run(run_id)
        except Exception as exc:
            raise CommandError(f"Client import run {run_id} failed to start") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Client import run {run.pk}: {run.status} "
                f"{run.processed_rows}/{run.total_rows}"
            )
        )
