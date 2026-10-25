from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ObjectDoesNotExist
from django.utils.dateparse import parse_datetime

from pool_service.communication_avito import AvitoError
from pool_service.communication_enrichment import enrich_conversation
from pool_service.communication_models import Conversation


class Command(BaseCommand):
    help = "Create conversation client cards and refresh bounded Avito chat metadata. Sends no messages."

    def add_arguments(self, parser):
        parser.add_argument("--ids", default="")
        parser.add_argument("--limit", type=int, default=20)
        parser.add_argument("--claim-time", default="")

    def handle(self, *args, **options):
        claim_time = parse_datetime(options["claim_time"]) if options["claim_time"] else None
        if options["claim_time"] and (claim_time is None or claim_time.tzinfo is None):
            raise CommandError("Invalid claim time")
        if options["ids"]:
            parts = options["ids"].split(",")
            if len(parts) > 20 or any(not value.isascii() or not value.isdigit() for value in parts):
                raise CommandError("Invalid conversation ids")
            ids = [int(value) for value in parts]
        else:
            ids = list(Conversation.objects.filter(client__isnull=True, connection__is_active=True,
                connection__channel__is_active=True).order_by("-last_message_at").values_list("pk", flat=True)[:max(1, min(options["limit"], 100))])
        completed = unavailable = 0
        captured = []
        for conversation_id in ids:
            try:
                enrich_conversation(conversation_id, claim_time=claim_time, refresh_context=False)
                captured.append(conversation_id)
                completed += 1
            except (AvitoError, ValueError, ObjectDoesNotExist):
                unavailable += 1
        # Cards for the whole batch are ready before bounded external reads.
        for conversation_id in captured[:5]:
            try:
                enrich_conversation(conversation_id, claim_time=claim_time)
            except (AvitoError, ValueError, ObjectDoesNotExist):
                unavailable += 1
        if claim_time and captured[5:]:
            Conversation.objects.filter(pk__in=captured[5:], context_attempted_at=claim_time).update(context_attempted_at=None)
        self.stdout.write(f"Conversation clients: completed={completed} unavailable={unavailable}")
