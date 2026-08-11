import logging

from django.core.management.base import BaseCommand
from django.db import close_old_connections

from pool_service.models import DevelopmentTask
from pool_service.services.development_codex import check_codex, dispatch_corrective_codex
from pool_service.services.development_db import database_error_code
from pool_service.services.development_review import run_review


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Продвигает автоматический цикл Codex → AI Review → корректировка."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=25)

    def handle(self, *args, **options):
        batch_size = max(1, min(options["batch_size"], 200))
        close_old_connections()
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
            task = None
            iteration_id = None
            stage = "load_task"
            # A management command has no request boundary to recycle stale
            # connections, so every task gets an explicit one.
            close_old_connections()
            try:
                task = DevelopmentTask.objects.get(pk=task_id)
                task_metadata = (
                    task.automation_metadata
                    if isinstance(task.automation_metadata, dict)
                    else {}
                )
                iteration_id = task_metadata.get("active_codex_iteration_id")
                if task.status in {DevelopmentTask.STATUS_CODEX_WORKING, DevelopmentTask.STATUS_BLOCKED}:
                    stage = "check_codex"
                    result = check_codex(task_id, None)
                    counts["checked"] += 1
                    stage = "refresh_after_codex_check"
                    task.refresh_from_db()
                    # Only validation failures are reviewable blocked results. Security and
                    # infrastructure failures remain stopped for a human.
                    if result.state not in {"completed", "no_changes", "validation_failed"} and task.status == DevelopmentTask.STATUS_BLOCKED:
                        continue
                if task.status in {DevelopmentTask.STATUS_REVIEW, DevelopmentTask.STATUS_BLOCKED}:
                    stage = "run_review"
                    result = run_review(task_id)
                    if result.changed:
                        counts["reviewed"] += 1
                    if result.review_id is not None:
                        iteration_id = result.review_id
                    stage = "refresh_after_review"
                    task.refresh_from_db()
                if task.status == DevelopmentTask.STATUS_REVISION:
                    stage = "select_corrective_review"
                    review = task.iterations.filter(
                        executor_type="chatgpt",
                        automation_metadata__purpose="ai_review",
                        automation_metadata__decision="corrective_required",
                    ).order_by("-id").first()
                    if review:
                        iteration_id = review.pk
                        stage = "dispatch_corrective_codex"
                        if dispatch_corrective_codex(task_id, review.pk).changed:
                            counts["corrective"] += 1
            except Exception as exc:
                counts["errors"] += 1
                logger.warning(
                    "Development Codex polling failed: task=%s stage=%s "
                    "iteration=%s error_type=%s db_error_code=%s",
                    task_id,
                    stage,
                    iteration_id,
                    type(exc).__name__,
                    database_error_code(exc),
                )
            finally:
                # Never let a broken connection from one task poison the next.
                close_old_connections()
        self.stdout.write("checked={checked} reviewed={reviewed} corrective={corrective} errors={errors}".format(**counts))
