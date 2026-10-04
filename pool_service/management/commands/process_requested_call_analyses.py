import time

from django.core.management.base import BaseCommand

from pool_service.communication_models import CallAnalysis
from pool_service.services.call_ai import process_call_analysis


class Command(BaseCommand):
    help = "Process call analyses explicitly requested by users."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=1)
        parser.add_argument("--idle-grace-seconds", type=float, default=0.0)

    def handle(self, *args, **options):
        limit = max(1, min(int(options["limit"]), 10))
        idle_grace = max(0.0, min(float(options["idle_grace_seconds"]), 5.0))
        attempted_ids = []
        processed = 0
        empty_checks = 0

        while len(attempted_ids) < limit:
            queryset = (
                CallAnalysis.objects.filter(
                    requested_at__isnull=False,
                    status=CallAnalysis.STATUS_PENDING,
                    call__recording_file__isnull=False,
                )
                .exclude(call__recording_file="")
                .order_by("requested_at", "pk")
            )
            if attempted_ids:
                queryset = queryset.exclude(call_id__in=attempted_ids)

            call_id = queryset.values_list("call_id", flat=True).first()
            if call_id is None:
                if idle_grace > 0 and empty_checks == 0:
                    empty_checks += 1
                    time.sleep(idle_grace)
                    continue
                break

            empty_checks = 0
            attempted_ids.append(call_id)
            if process_call_analysis(call_id):
                processed += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Requested call analyses processed: {processed}/{len(attempted_ids)}"
            )
        )
