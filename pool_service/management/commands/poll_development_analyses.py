import logging

from django.core.management.base import BaseCommand

from pool_service.models import DevelopmentIteration, DevelopmentTask
from pool_service.services.development_ai import check_analysis


logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Проверяет незавершённые фоновые AI-анализы задач разработки."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=50)

    def handle(self, *args, **options):
        batch_size = max(1, min(options["batch_size"], 500))
        iteration_ids = list(
            DevelopmentIteration.objects.filter(
                task__status=DevelopmentTask.STATUS_ANALYSIS,
                executor_type=DevelopmentIteration.EXECUTOR_SYSTEM,
                status=DevelopmentIteration.STATUS_WORKING,
                automation_metadata__purpose="primary_analysis",
            )
            .exclude(automation_metadata__response_id="")
            .order_by("id")
            .values_list("id", flat=True)[:batch_size]
        )
        counts = {"checked": 0, "completed": 0, "pending": 0, "errors": 0}
        for iteration_id in iteration_ids:
            try:
                result = check_analysis(iteration_id)
                counts["checked"] += 1
                if result.state == "completed":
                    counts["completed"] += 1
                elif result.state == "check_failed":
                    counts["errors"] += 1
                else:
                    counts["pending"] += 1
            except Exception as exc:
                counts["errors"] += 1
                logger.warning(
                    "Development analysis polling failed: iteration=%s error_type=%s",
                    iteration_id,
                    type(exc).__name__,
                )
        self.stdout.write(
            "checked={checked} completed={completed} pending={pending} errors={errors}".format(**counts)
        )
