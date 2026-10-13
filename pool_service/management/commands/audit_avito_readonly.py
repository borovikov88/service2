import json
from django.core.management.base import BaseCommand, CommandError
from pool_service.avito_readonly_audit import collect, parse_item_ids
from scripts.avito_readonly_receipt import FRAME

class Command(BaseCommand):
    help = "Explicit hosting-operator read of selected Avito links and last v4 report; no listing writes."

    def add_arguments(self, parser):
        parser.add_argument("--item-ids", required=True)

    def handle(self, *args, **options):
        try:
            item_ids = parse_item_ids(options["item_ids"])
        except ValueError:
            raise CommandError("item IDs invalid") from None
        try:
            report = collect(item_ids)
        except Exception:
            raise CommandError("Avito read-only audit unavailable; raw error withheld") from None
        self.stdout.write(FRAME + json.dumps(report, ensure_ascii=False, allow_nan=False))
