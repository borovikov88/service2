from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from pool_service.models import CrmItem, ServiceTask


class Command(BaseCommand):
    help = "Удаляет навсегда архивные записи с причиной deleted старше указанного числа дней."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Возраст архивной deleted-записи в днях для удаления. По умолчанию 30.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Только показать, сколько записей будет удалено, без фактического удаления.",
        )

    def handle(self, *args, **options):
        days = max(int(options["days"]), 0)
        dry_run = bool(options["dry_run"])
        threshold = timezone.now() - timedelta(days=days)

        task_qs = ServiceTask.objects.filter(
            is_archived=True,
            archived_reason=ServiceTask.ARCHIVE_REASON_DELETED,
            archived_at__lte=threshold,
        )
        crm_qs = CrmItem.objects.filter(
            is_archived=True,
            archived_reason=CrmItem.ARCHIVE_REASON_DELETED,
            archived_at__lte=threshold,
        )

        task_count = task_qs.count()
        crm_count = crm_qs.count()

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: tasks={task_count}, crm={crm_count}, threshold={threshold:%d.%m.%Y %H:%M}"
                )
            )
            return

        deleted_tasks, _ = task_qs.delete()
        deleted_crm, _ = crm_qs.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Удалено из архива: tasks={task_count}, crm={crm_count}, threshold={threshold:%d.%m.%Y %H:%M}"
            )
        )
