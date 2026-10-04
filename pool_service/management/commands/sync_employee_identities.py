from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pool_service.models import Organization
from pool_service.services.employee_identity_sync import (
    EmployeeIdentitySyncError,
    sync_all_employee_identities,
)


class Command(BaseCommand):
    help = "Synchronize employee identities across 1C, Service2 and MegaFon."

    def handle(self, *args, **options):
        raw_org_id = str(
            getattr(settings, "ONEC_ODATA_TARGET_ORGANIZATION_ID", "") or ""
        ).strip()
        if not raw_org_id.isdigit() or int(raw_org_id) <= 0:
            raise CommandError("ONEC_ODATA_TARGET_ORGANIZATION_ID is not configured.")
        organization = Organization.objects.filter(pk=int(raw_org_id)).first()
        if organization is None:
            raise CommandError("Configured organization was not found.")

        try:
            result = sync_all_employee_identities(organization)
        except EmployeeIdentitySyncError as exc:
            raise CommandError("; ".join(exc.messages)) from exc

        telephony_synced = sum(
            item["synced"] for item in result["telephony"]
        )
        telephony_unmapped = sum(
            item["needs_mapping"] for item in result["telephony"]
        )
        telephony_errors = [
            item for item in result["telephony"] if item.get("error")
        ]
        summary = (
            "Employee identity sync completed: "
            f"1C={result['onec']['synced']} "
            f"MegaFon={telephony_synced} "
            f"MegaFon_unmapped={telephony_unmapped} "
            f"MegaFon_errors={len(telephony_errors)}"
        )
        summary_style = self.style.WARNING if telephony_errors else self.style.SUCCESS
        self.stdout.write(summary_style(summary))
        for item in telephony_errors:
            self.stderr.write(
                self.style.WARNING(
                    f"MegaFon line skipped: {item['name']}: {item['error']}"
                )
            )
        if telephony_errors:
            raise CommandError(
                f"Employee identity sync failed for {len(telephony_errors)} MegaFon line(s)."
            )
