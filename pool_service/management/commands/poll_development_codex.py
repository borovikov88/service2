import logging

from django.core.management.base import BaseCommand

from pool_service.models import DevelopmentTask
from pool_service.services.development_codex import check_codex, dispatch_corrective_codex
from pool_service.services.development_review import run_review


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Продвигает автоматический цикл Codex → AI Review → корректировка."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=25)

    def handle(self, *args, **options):
        batch_size = max(1, min(options["batch_size"], 200))
        ids = list(
            DevelopmentTask.objects.filter(status__in=[
                DevelopmentTask.STATUS_CODEX_WORKING,
                DevelopmentTask.STATUS_REVIEW,
                DevelopmentTask.STATUS_REVISION,
                DevelopmentTask.STATUS_BLOCKED,
            ]).order_by("id").values_list("id", flat=True)[:batch_size]
        )
        counts = {"checked": 0, "reviewed": 0, "corrective": 0, "errors": 0}
        for task_id in ids:
            try:
                task = DevelopmentTask.objects.get(pk=task_id)
                if task.status in {DevelopmentTask.STATUS_CODEX_WORKING, DevelopmentTask.STATUS_BLOCKED}:
                    result = check_codex(task_id, None)
                    counts["checked"] += 1
                    task.refresh_from_db()
                    # Only validation failures are reviewable blocked results. Security and
                    # infrastructure failures remain stopped for a human.
                    if result.state not in {"completed", "no_changes", "validation_failed"} and task.status == DevelopmentTask.STATUS_BLOCKED:
                        continue
                if task.status in {DevelopmentTask.STATUS_REVIEW, DevelopmentTask.STATUS_BLOCKED}:
                    result = run_review(task_id)
                    if result.changed:
                        counts["reviewed"] += 1
                    task.refresh_from_db()
                if task.status == DevelopmentTask.STATUS_REVISION:
                    review = task.iterations.filter(
                        executor_type="chatgpt",
                        automation_metadata__purpose="ai_review",
                        automation_metadata__decision="corrective_required",
                    ).order_by("-id").first()
                    if review and dispatch_corrective_codex(task_id, review.pk).changed:
                        counts["corrective"] += 1
            except Exception as exc:
                counts["errors"] += 1
                logger.warning("Development Codex polling failed: task=%s error_type=%s", task_id, type(exc).__name__)
        self.stdout.write("checked={checked} reviewed={reviewed} corrective={corrective} errors={errors}".format(**counts))
