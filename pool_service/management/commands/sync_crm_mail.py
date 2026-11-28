import time

from django.core.management.base import BaseCommand
from django.db.models import F

from pool_service.mailbox_models import CrmMailbox
from pool_service.services.mailbox_sync import sync_mailbox


class Command(BaseCommand):
    help = "Sync organization mailboxes, oldest attempt first, within the cron time budget."

    def add_arguments(self, parser):
        parser.add_argument("--organization", type=int, required=True)

    def handle(self, *args, **options):
        boxes = CrmMailbox.objects.filter(organization_id=options["organization"], enabled=True)
        ids = list(boxes.order_by(F("last_attempt_at").asc(nulls_first=True), "pk").values_list("pk", flat=True))
        deadline = time.monotonic() + 35
        for pk in ids:
            remaining = deadline - time.monotonic()
            if remaining < 15:
                break
            sync_mailbox(pk, seconds=int(remaining))
