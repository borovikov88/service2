"""Cron entrypoint. Only due, explicitly enabled owner subscriptions are read."""
import time
from collections import Counter
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from pool_service.avito_status_monitor import SCAN_SECONDS, scan_monitor
from pool_service.communication_models import AvitoStatusMonitor


class Command(BaseCommand):
    help = "Check due Avito listing statuses; output sanitized aggregate counts only."

    def add_arguments(self, parser):
        parser.add_argument("--status", action="store_true")
        parser.add_argument("--limit", type=int, default=4)
        parser.add_argument("--budget-seconds", type=int, default=1500)

    def handle(self, *args, **options):
        if options["status"]:
            monitors = AvitoStatusMonitor.objects.all()
            now = timezone.now()
            self.stdout.write("AVITO_STATUS_MONITOR_READY " + " ".join((
                f"configured={monitors.count()}", f"enabled={monitors.filter(enabled=True).count()}",
                f"baselined={monitors.filter(enabled=True, baseline_at__isnull=False).count()}",
                f"due={monitors.filter(enabled=True, next_due_at__lte=now).count()}",
                f"running={monitors.filter(enabled=True, lease_until__gt=now).count()}",
                f"failed={monitors.filter(enabled=True).exclude(last_error_code='').count()}",
            )))
            return
        limit, budget = options["limit"], options["budget_seconds"]
        if not 1 <= limit <= 10 or not SCAN_SECONDS + 30 <= budget <= 1500:
            raise CommandError("Expected --limit 1..10 and --budget-seconds 450..1500")
        deadline = time.monotonic() + budget
        ids = list(AvitoStatusMonitor.objects.filter(enabled=True, next_due_at__lte=timezone.now())
                   .order_by("next_due_at", "pk").values_list("pk", flat=True)[:limit])
        counts = Counter()
        for monitor_id in ids:
            if deadline - time.monotonic() < SCAN_SECONDS + 20:
                break
            try:
                status, notifications = scan_monitor(monitor_id)
            except Exception:
                # The transaction rolls back. Its lease expires for safe retry;
                # do not leak a database/provider exception into public job logs.
                status, notifications = "failed", 0
            counts[status] += 1
            counts["notifications"] += notifications
        self.stdout.write("AVITO_STATUS_MONITOR " + " ".join(
            f"{key}={counts[key]}" for key in ("baseline", "success", "failed", "skipped", "notifications")
        ))
        if counts["failed"]:
            raise CommandError("Avito status scan incomplete; inspect owner-only monitor state in Service2.")
