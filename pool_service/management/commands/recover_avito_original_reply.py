import json
from django.core.management.base import BaseCommand, CommandError
from pool_service.avito_outbox_original_recovery import recover
from scripts.avito_outbox_recovery_receipt import FRAME


class Command(BaseCommand):
    help = "One marker-authorized original Services reply; never a general replay."

    def add_arguments(self, parser):
        parser.add_argument("--expected-sha", required=True)

    def handle(self, *args, **options):
        try:
            result = recover(expected_sha=options["expected_sha"])
        except Exception:
            raise CommandError("Original Services recovery unavailable; private error withheld") from None
        self.stdout.write(FRAME + json.dumps(result, ensure_ascii=False, allow_nan=False))
