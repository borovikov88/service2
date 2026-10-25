import json
from django.core.management.base import BaseCommand, CommandError

from pool_service.avito_outbox import (
    claim_message, configure_outbox, outbox_diagnostics, reconcile_message, run_outbox,
)
from pool_service.communication_avito import AvitoError, send_message
from pool_service.communication_models import ChannelConnection

MAX_TRANSIENT_ATTEMPTS = 3


class Command(BaseCommand):
    help = "Process one explicitly selected Avito connection; old backlog is held."

    def add_arguments(self, parser):
        parser.add_argument("--connection-id", type=int, required=True)
        parser.add_argument("--message-id", type=int)
        parser.add_argument("--limit", type=int, default=10)
        parser.add_argument("--status", action="store_true")
        parser.add_argument("--configure", action="store_true")
        parser.add_argument("--account-id")
        parser.add_argument("--reconcile-only", action="store_true")
        parser.add_argument("--periodic", action="store_true")

    def handle(self, *args, **options):
        connection_id = options["connection_id"]
        if sum(bool(options[name]) for name in ("status", "configure", "reconcile_only")) > 1:
            raise CommandError("outbox_invalid_mode")
        try:
            connection = ChannelConnection.objects.select_related("channel").get(pk=connection_id)
            if not outbox_diagnostics(connection)["binding_valid"]:
                raise CommandError("outbox_scope_invalid")
            if options["status"]:
                result = outbox_diagnostics(connection)
            elif options["configure"]:
                if not options["account_id"] or options["message_id"]:
                    raise CommandError("outbox_invalid_mode")
                result = configure_outbox(connection_id, options["account_id"])
            elif options["reconcile_only"]:
                if options["message_id"] is None:
                    raise CommandError("outbox_message_required")
                result = reconcile_message(connection_id, options["message_id"])
                result["provider_id_present"] = bool(result.pop("provider_id", None))
            else:
                result = run_outbox(connection_id, message_id=options["message_id"],
                    limit=options["limit"], explicit=options["message_id"] is not None, sender=send_message,
                    periodic=options["periodic"])
        except ChannelConnection.DoesNotExist:
            raise CommandError("outbox_scope_invalid") from None
        except AvitoError as error:
            raise CommandError(str(error)) from None
        self.stdout.write(json.dumps(result, sort_keys=True))
