"""Cron entrypoint. Only due, explicitly enabled self-subscriptions are read."""
import time
from collections import Counter
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from pool_service.avito_status_monitor import SCAN_SECONDS, scan_monitor
from pool_service.communication_models import AvitoStatusMonitor, ChannelConnection
from pool_service import avito_autoload_auto, avito_advance, avito_tariff_reminder, avito_runtime, avito_deferred_messages, avito_daily_statistics
from pool_service.avito_scheduler import scheduler_state


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
            scheduler = scheduler_state()
            # Keep stdout exactly compatible with the cron readiness parser.
            self.stderr.write(f"AVITO_SCHEDULER code={scheduler['code']} completed_worker={str(scheduler['worker_ready']).lower()}")
            for connection in ChannelConnection.objects.filter(channel__kind="avito", is_active=True).select_related("channel")[:10]:
                for item in avito_runtime.checks(connection):
                    outcome = item["last_worker_outcome"] or "unknown"
                    code = item["last_worker_code"] if item["last_worker_verified"] else "worker_result_unknown"
                    self.stderr.write(f"AVITO_CHECK connection={connection.pk} check={item['check']} outcome={outcome} code={code}")
            return
        limit, budget = options["limit"], options["budget_seconds"]
        if not 1 <= limit <= 10 or not SCAN_SECONDS + 30 <= budget <= 1500:
            raise CommandError("Expected --limit 1..10 and --budget-seconds 450..1500")
        deadline = time.monotonic() + budget
        ids = list(AvitoStatusMonitor.objects.filter(enabled=True, next_due_at__lte=timezone.now())
                   .order_by("next_due_at", "pk").values_list("pk", flat=True)[:limit])
        counts = Counter()
        run_id = avito_runtime.worker_context()
        def observed(connection_id, check, outcome, **kwargs):
            try:
                avito_runtime.record(connection_id, check, outcome, run_id=run_id, **kwargs)
            except Exception:
                counts["diagnostic_failed"] += 1
            if check == "publication_correlation" and outcome == "failed":
                counts["correlation_failed"] += 1
        # Explicitly mark due work deferred by shared budget/batch bounds.
        for connection in ChannelConnection.objects.filter(channel__kind="avito", is_active=True).select_related("channel")[:10]:
            for item in avito_runtime.checks(connection):
                if item["enabled"] and (not item["next_due_at"] or avito_runtime.when(item["next_due_at"]) <= timezone.now()):
                    avito_runtime.record(connection.pk, item["check"], "not_run", run_id=run_id, deferred=True)
        deferred = avito_deferred_messages.scan_due(observer=observed)
        # Reserve one full status scan before spending the shared budget on reports.
        report_deadline = deadline - (SCAN_SECONDS + 20 if ids else 0)
        # Calendar reminders survive provider access loss at tariff expiry.
        tariffs = avito_tariff_reminder.scan_due(deadline=report_deadline, limit=4, observer=observed)
        advances = avito_advance.scan_due(deadline=report_deadline, limit=2, observer=observed)
        reports = avito_autoload_auto.scan_due(deadline=report_deadline, limit=2, observer=observed)
        statistics = avito_daily_statistics.scan_due(deadline=report_deadline, observer=observed)
        for monitor_id in ids:
            if deadline - time.monotonic() < SCAN_SECONDS + 20:
                break
            failed_with_exception = False
            try:
                status, notifications = scan_monitor(monitor_id)
            except Exception:
                # The transaction rolls back. Its lease expires for safe retry;
                # do not leak a database/provider exception into public job logs.
                status, notifications = "failed", 0
                failed_with_exception = True
            connection_id = AvitoStatusMonitor.objects.filter(pk=monitor_id).values_list("connection_id", flat=True).first()
            if connection_id:
                observed(connection_id, "statuses", status, exception=failed_with_exception)
            counts[status] += 1
            counts["notifications"] += notifications
        self.stdout.write("AVITO_STATUS_MONITOR " + " ".join(
            f"{key}={counts[key]}" for key in ("baseline", "success", "failed", "skipped", "notifications")
        ))
        self.stdout.write("AVITO_AUTOLOAD_REPORT " + " ".join(
            f"{key}={reports[key]}" for key in ("success", "failed", "skipped")))
        self.stdout.write("AVITO_ADVANCE_MONITOR " + " ".join(
            f"{key}={advances[key]}" for key in ("success", "failed", "skipped")))
        self.stdout.write("AVITO_TARIFF_REMINDER " + " ".join(
            f"{key}={tariffs[key]}" for key in ("success", "failed", "skipped")))
        self.stdout.write("AVITO_DAILY_STATISTICS " + " ".join(
            f"{key}={statistics[key]}" for key in ("success", "failed", "skipped")))
        if counts["failed"] or counts["correlation_failed"] or counts["diagnostic_failed"] or deferred["failed"] or reports["failed"] or advances["failed"] or tariffs["failed"] or statistics["failed"]:
            # Distinguish a completed tick with scoped failures from a crash, timeout
            # or skipped/locked tick. Only the supervisor records the actual code.
            raise CommandError("Avito checks incomplete; inspect scoped state in Service2.", returncode=2)
