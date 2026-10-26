"""Protected one-time setup for the explicitly authorised services account."""
import json
import os
import re
import subprocess

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pool_service.avito_services_workplace_setup import setup, safe_setup_error


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--expected-sha", required=True)
        parser.add_argument("--base-url", required=True)

    def handle(self, *args, **options):
        expected = options["expected_sha"]
        if os.environ.get("SERVICE2_AVITO_DEPLOY_SETUP") != "1" or not re.fullmatch(r"[a-f0-9]{40}", expected):
            raise CommandError("services_setup_protected_deploy_required")
        actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=settings.BASE_DIR,
            check=True, capture_output=True, text=True, timeout=10).stdout.strip()
        if actual != expected:
            raise CommandError("services_setup_commit_mismatch")
        try:
            result = setup(4, "446312402", options["base_url"])
        except Exception as exc:
            result = {"setup": "blocked", "connection_id": 4, "code": safe_setup_error(exc)}
        if result.get("setup") != "blocked":
            from pool_service.avito_services_content_evidence import record_owner_photo_report_once
            result["manual_photo_evidence"] = record_owner_photo_report_once()
            from pool_service.avito_daily_statistics import setup_once
            try:
                result["daily_statistics"] = setup_once()
            except Exception:
                result["daily_statistics"] = {"state": "blocked", "code": "statistics_setup_unavailable",
                    "connection_id": 4, "account_id": "446312402"}
            from pool_service.crm_source_links import setup_evidence as crm_source_evidence
            try:
                result["crm_source_links"] = crm_source_evidence()
            except Exception:
                result["crm_source_links"] = {"state": "blocked", "code": "crm_source_evidence_unavailable",
                    "connection_id": 4, "account_id": "446312402"}
            from pool_service.avito_improvement_proposals import setup_owner_cycle_once
            result["improvement_projects"] = setup_owner_cycle_once()
            from pool_service.avito_owner_review import safe_owner_review_receipt
            result["owner_review"] = safe_owner_review_receipt()
            from pool_service.avito_comparable_subsets import safe_setup_evidence as comparable_subset_evidence
            result["comparable_subsets"] = comparable_subset_evidence()
            from pool_service import avito_tariff_subscription
            from pool_service.avito_services_workplace_setup import _actor, _connection
            try:
                result["tariff_subscription"] = avito_tariff_subscription.setup_evidence(_actor(_connection(4)))
            except Exception:
                result["tariff_subscription"] = {"state": "blocked", "code": "tariff_scope_changed",
                    "connection_id": 4, "account_id": "446312402", "request_performed": False}
        self.stdout.write("AVITO_SERVICES_SETUP " + json.dumps(result, sort_keys=True))
