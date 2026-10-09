"""Internal bounded supervisor helper; never used by readiness or web requests."""
import os
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.utils import timezone
from pool_service.avito_scheduler import HEARTBEAT_KEY
from pool_service.communication_models import AvitoSchedulerHeartbeat


class Command(BaseCommand):
    help = "Record sanitized evidence from the Avito cron supervisor."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("phase", choices=("begin", "finish"))
        parser.add_argument("--run-id", type=uuid.UUID, required=True)
        parser.add_argument("--exit-code", type=int)

    def handle(self, *args, **options):
        if os.environ.get("SERVICE2_AVITO_CRON_SUPERVISED") != "1":
            raise CommandError("supervisor_required")
        phase, code = options["phase"], options["exit_code"]
        if (phase == "begin" and code is not None) or (phase == "finish" and (code is None or not -255 <= code <= 255)):
            raise CommandError("invalid_heartbeat_arguments")
        now = timezone.now()
        try:
            if phase == "begin":
                AvitoSchedulerHeartbeat.objects.update_or_create(
                    pk=HEARTBEAT_KEY,
                    defaults={"run_id": options["run_id"], "state": "running",
                              "started_at": now, "finished_at": None, "exit_code": None},
                )
            else:
                # A late completion cannot overwrite a newer supervisor run.
                updated = AvitoSchedulerHeartbeat.objects.filter(
                    pk=HEARTBEAT_KEY, run_id=options["run_id"], state="running",
                    started_at__lte=now,
                ).update(state="finished", finished_at=now, exit_code=code)
                if updated != 1:
                    raise CommandError("heartbeat_run_mismatch")
        except DatabaseError:
            raise CommandError("heartbeat_storage_failed") from None
