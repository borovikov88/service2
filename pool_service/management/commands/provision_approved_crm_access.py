"""Protected, dry-run-by-default execution of a single CRM access approval."""
import json
import os
import re
import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pool_service.services.crm_approved_access import APPROVAL_ID, ProvisioningBlocked, provision


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--expected-sha", required=True)
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--approval-id")
        parser.add_argument("--run-id")
        parser.add_argument("--run-attempt")

    def handle(self, *args, **options):
        expected = options["expected_sha"]
        if os.environ.get("SERVICE2_CRM_APPROVED_ACCESS") != "1" or not re.fullmatch(r"[a-f0-9]{40}", expected):
            raise CommandError("crm_access_protected_deploy_required")
        if options["approval_id"] not in (None, APPROVAL_ID) or (options["apply"] and options["approval_id"] != APPROVAL_ID):
            raise CommandError("crm_access_approval_required")
        context = {}
        for field in ("run_id", "run_attempt"):
            value = options[field]
            if value is None and not options["apply"]:
                context[field] = 0
            elif not isinstance(value, str) or not re.fullmatch(
                    r"[1-9][0-9]{0,19}" if field == "run_id" else r"[1-9][0-9]{0,5}", value):
                raise CommandError("crm_access_execution_context_required")
            else:
                context[field] = int(value)
        try:
            actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=settings.BASE_DIR,
                check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise CommandError("crm_access_commit_unavailable") from None
        if actual != expected:
            raise CommandError("crm_access_commit_mismatch")
        try:
            result = provision(expected_sha=expected, apply=options["apply"], **context)
        except ProvisioningBlocked as exc:
            raise CommandError(str(exc)) from None
        except Exception:
            # SQL/validation exceptions may contain private row values. The atomic
            # service has already rolled back before this fixed error is emitted.
            raise CommandError("crm_access_operation_failed") from None
        self.stdout.write("CRM_APPROVED_ACCESS " + json.dumps(result, sort_keys=True))
