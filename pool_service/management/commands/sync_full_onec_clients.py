from django.core.management.base import BaseCommand

from pool_service.client_crm_import import sync_full_onec_clients


class Command(BaseCommand):
    help = "Run full nightly reconciliation of 1C buyers with Service2 CRM clients."

    def handle(self, *args, **options):
        result = sync_full_onec_clients()
        if result.get("skipped"):
            self.stdout.write(
                self.style.WARNING(
                    "Full 1C client sync skipped: manual import is active."
                )
            )
            return

        self.stdout.write(
            self.style.SUCCESS(
                "Full 1C client sync: "
                f"scanned={result['scanned']} "
                f"imported={result['imported']} "
                f"review_failed={result['review_failed']} "
                f"refreshed={result['refreshed']} "
                f"refresh_failed={result['refresh_failed']}"
            )
        )
