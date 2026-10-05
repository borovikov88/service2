import json

from django.core.management.base import BaseCommand

from pool_service.client_crm_sync import (
    RECENT_LOOKBACK_HOURS,
    sync_all_onec_clients,
    sync_recent_onec_clients,
)


class Command(BaseCommand):
    help = "Synchronize Service2 CRM clients from 1C."

    def add_arguments(self, parser):
        parser.add_argument(
            "--mode",
            choices=("incremental", "full"),
            default="incremental",
        )
        parser.add_argument(
            "--lookback-hours",
            type=int,
            default=RECENT_LOOKBACK_HOURS,
        )

    def handle(self, *args, **options):
        mode = options["mode"]
        if mode == "full":
            result = sync_all_onec_clients()
        else:
            lookback_hours = max(1, min(int(options["lookback_hours"]), 168))
            result = sync_recent_onec_clients(lookback_hours=lookback_hours)

        self.stdout.write(
            json.dumps(
                {"mode": mode, "result": result},
                ensure_ascii=False,
                default=str,
            )
        )
