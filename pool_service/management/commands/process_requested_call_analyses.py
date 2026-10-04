from django.core.management.base import BaseCommand

from pool_service.communication_models import CallAnalysis
from pool_service.services.call_ai import process_call_analysis


class Command(BaseCommand):
    help = "Process call analyses explicitly requested by users."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=1)

    def handle(self, *args, **options):
        limit = max(1, min(int(options["limit"]), 10))
        call_ids = list(
            CallAnalysis.objects.filter(
                requested_at__isnull=False,
                status=CallAnalysis.STATUS_PENDING,
                call__recording_file__isnull=False,
            )
            .exclude(call__recording_file="")
            .order_by("requested_at", "pk")
            .values_list("call_id", flat=True)[:limit]
        )

        processed = 0
        for call_id in call_ids:
            if process_call_analysis(call_id):
                processed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Requested call analyses processed: {processed}/{len(call_ids)}"
            )
        )
