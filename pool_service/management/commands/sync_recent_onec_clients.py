from django.core.management.base import BaseCommand

from pool_service.client_crm_import import sync_recent_onec_clients


class Command(BaseCommand):
    help = "Import newly created 1C buyers into Service2 without a full catalog scan."

    def add_arguments(self, parser):
        parser.add_argument(
            "--lookback-hours",
            type=int,
            default=48,
            help="Overlap window for recently created 1C buyers.",
        )

    def handle(self, *args, **options):
        result = sync_recent_onec_clients(
            lookback_hours=max(int(options["lookback_hours"]), 1)
        )
        if result.get("skipped"):
            self.stdout.write(
                self.style.WARNING(
                    "Recent 1C client sync skipped: manual import is active."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                "Recent 1C client sync: "
                f"scanned={result['scanned']} "
                f"imported={result['imported']} "
                f"refreshed={result['refreshed']} "
                f"review={result['review']} "
                f"duplicate={result['duplicate']} "
                f"invalid={result['invalid']} "
                f"skipped={result['skipped']}"
            )
        )
