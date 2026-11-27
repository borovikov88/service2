from django.core.management.base import BaseCommand

from pool_service.mailbox_models import CrmMailbox
from pool_service.services.mailbox_sync import sync_mailbox


class Command(BaseCommand):
    help = "Sync VK WorkSpace mailbox headers without creating CRM cards."

    def add_arguments(self, parser):
        parser.add_argument("--organization", type=int, required=True)

    def handle(self, *args, **options):
        boxes = CrmMailbox.objects.filter(organization_id=options["organization"], enabled=True)
        for pk in boxes.values_list("pk", flat=True):
            sync_mailbox(pk)
