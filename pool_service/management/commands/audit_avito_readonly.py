import json
from django.core.management.base import BaseCommand, CommandError
from pool_service.avito_readonly_audit import (
    collect_services, parse_item_ids, SERVICES_CONNECTION_ID, SERVICES_ACCOUNT_ID, SERVICES_REVISION,
)
from scripts.avito_readonly_receipt import FRAME


class Command(BaseCommand):
    help = "Protected read-only release evidence for services connection4/account446312402."

    def add_arguments(self, parser):
        parser.add_argument("--item-ids", default="")
        parser.add_argument("--connection-id", type=int, default=SERVICES_CONNECTION_ID)
        parser.add_argument("--account-id", default=SERVICES_ACCOUNT_ID)
        parser.add_argument("--revision", type=int, default=SERVICES_REVISION)

    def handle(self, *args, **options):
        if (options["connection_id"] != SERVICES_CONNECTION_ID
                or options["account_id"] != SERVICES_ACCOUNT_ID
                or options["revision"] != SERVICES_REVISION):
            raise CommandError("Avito services audit scope invalid")
        try:
            item_ids = parse_item_ids(options["item_ids"]) if options["item_ids"] else []
        except ValueError:
            raise CommandError("item IDs invalid") from None
        try:
            report = collect_services(item_ids, connection_id=options["connection_id"],
                account_id=options["account_id"], revision=options["revision"])
        except Exception:
            raise CommandError("Avito read-only audit unavailable; raw error withheld") from None
        self.stdout.write(FRAME + json.dumps(report, ensure_ascii=False, allow_nan=False))
