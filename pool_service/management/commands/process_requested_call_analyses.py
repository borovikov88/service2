import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from pool_service.communication_models import CallAnalysis
from pool_service.services.call_ai import (
    DEFAULT_MAX_ATTEMPTS,
    PROCESSING_STALE_MINUTES,
    process_call_analysis,
)


def recover_stale_requested_analyses():
    stale_before = timezone.now() - timedelta(minutes=PROCESSING_STALE_MINUTES)
    max_attempts = int(
        getattr(settings, "OPENAI_CALL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)
    )
    stale = CallAnalysis.objects.filter(
        requested_at__isnull=False,
        status=CallAnalysis.STATUS_PROCESSING,
        processing_started_at__lt=stale_before,
    )

    failed = stale.filter(attempts__gte=max_attempts).update(
        status=CallAnalysis.STATUS_FAILED,
        error="processing_stale_attempt_limit",
        processing_token="",
        processing_started_at=None,
        processed_at=timezone.now(),
        requested_at=None,
    )
    recovered = stale.filter(attempts__lt=max_attempts).update(
        status=CallAnalysis.STATUS_PENDING,
        error="processing_stale_requeued",
        processing_token="",
        processing_started_at=None,
    )
    return recovered, failed


class Command(BaseCommand):
    help = "Process call analyses explicitly requested by users."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=1)
        parser.add_argument("--idle-grace-seconds", type=float, default=0.0)
        parser.add_argument("--concurrency", type=int, default=1)
        parser.add_argument("--drain", action="store_true")

    def handle(self, *args, **options):
        limit = max(1, min(int(options["limit"]), 10))
        idle_grace = max(0.0, min(float(options["idle_grace_seconds"]), 5.0))
        concurrency = max(1, min(int(options["concurrency"]), 4))
        drain = bool(options["drain"])
        attempted_ids = []
        processed = 0
        empty_checks = 0
        recovered, failed_stale = recover_stale_requested_analyses()
        if recovered or failed_stale:
            self.stdout.write(
                f"Recovered stale call analyses: {recovered}; "
                f"failed at attempt limit: {failed_stale}"
            )

        def process_one(call_id):
            close_old_connections()
            try:
                return bool(process_call_analysis(call_id))
            finally:
                close_old_connections()

        while drain or len(attempted_ids) < limit:
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

            batch_size = concurrency
            if not drain:
                batch_size = min(batch_size, limit - len(attempted_ids))
            call_ids = list(
                queryset.values_list("call_id", flat=True)[:batch_size]
            )
            if not call_ids:
                if idle_grace > 0 and empty_checks == 0:
                    empty_checks += 1
                    time.sleep(idle_grace)
                    continue
                break

            empty_checks = 0
            attempted_ids.extend(call_ids)
            if len(call_ids) == 1:
                processed += int(process_one(call_ids[0]))
            else:
                with ThreadPoolExecutor(max_workers=len(call_ids)) as executor:
                    processed += sum(executor.map(process_one, call_ids))

        self.stdout.write(
            self.style.SUCCESS(
                f"Requested call analyses processed: {processed}/{len(attempted_ids)}"
            )
        )
