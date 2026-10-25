import json
from django.core.management.base import BaseCommand, CommandError
from pool_service.avito_outbox_audit import collect
from scripts.avito_outbox_receipt import FRAME


class Command(BaseCommand):
    help = "Read-only exact Services outbox incident evidence; no customer send."

    def add_arguments(self, parser):
        parser.add_argument("--connection-id", type=int, required=True)
        parser.add_argument("--account-id", required=True)

    def handle(self, *args, **options):
        try:
            result = collect(connection_id=options["connection_id"], account_id=options["account_id"])
        except Exception:
            raise CommandError("Services outbox audit unavailable; private error withheld") from None
        self.stdout.write(FRAME + json.dumps(result, ensure_ascii=False, allow_nan=False))
