"""Operator-local bounded inbox operations; no HTTP grants or consumer loading."""
import json

from django.core.management.base import BaseCommand, CommandError

from pool_service.services.crm_inbox import inbox_status, reconcile, retry_failed, run_worker


class Command(BaseCommand):
    help = "Scoped CRM inbox worker, reconciliation, retry or status (no scheduler)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["work", "reconcile", "retry", "status"])
        parser.add_argument("--organization", type=int, required=True)
        parser.add_argument("--source-type", choices=["message", "call"], required=True)
        parser.add_argument("--connection", type=int, required=True)
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--seconds", type=float, default=10)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--include-legacy", action="store_true")
        parser.add_argument("--after-id", type=int, default=0)
        parser.add_argument("--through-id", type=int)

    def handle(self, *args, **options):
        scope = dict(organization_id=options["organization"], source_type=options["source_type"], connection_id=options["connection"])
        try:
            # All actions validate the currently active scoped source connection.
            status = inbox_status(**scope)
            if options["action"] == "status":
                result = status
            elif options["action"] == "reconcile":
                result = reconcile(**scope, limit=options["limit"], seconds=options["seconds"], dry_run=options["dry_run"],
                    include_legacy=options["include_legacy"], after_id=options["after_id"], through_id=options["through_id"])
            elif options["dry_run"]:
                raise CommandError("dry_run_requires_reconcile")
            elif options["action"] == "retry":
                result = {"retried": retry_failed(**scope, limit=options["limit"])}
            else:
                result = run_worker(**scope, limit=options["limit"], seconds=options["seconds"])
        except ValueError as exc:
            raise CommandError(str(exc)) from None
        self.stdout.write(json.dumps(result, default=str, sort_keys=True))
        if result.get("error_code"):
            raise CommandError(result["error_code"])
