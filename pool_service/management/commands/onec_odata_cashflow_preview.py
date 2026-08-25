import json
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pool_service.finance_imports.odata_cashflow import read_cashflow_rows
from pool_service.finance_imports.odata_profit import ODataConfig, ODataPreviewError


class Command(BaseCommand):
    help = "Read-only preview of 1C Fresh OData cash-flow aggregates"

    def add_arguments(self, parser):
        parser.add_argument("--start-month", required=True)
        parser.add_argument("--end-month", required=True)

    def handle(self, *args, **options):
        config = ODataConfig(
            base_url=settings.ONEC_ODATA_BASE_URL,
            username=settings.ONEC_ODATA_USERNAME,
            password=settings.ONEC_ODATA_PASSWORD,
            organization_guids=tuple(settings.ONEC_ODATA_ORGANIZATION_GUIDS),
            timeout_seconds=settings.ONEC_ODATA_TIMEOUT_SECONDS,
            max_pages=settings.ONEC_ODATA_MAX_PAGES,
            max_rows=settings.ONEC_ODATA_MAX_ROWS,
        )
        try:
            rows, page_count = read_cashflow_rows(
                config,
                options["start_month"],
                options["end_month"],
            )
        except ODataPreviewError as exc:
            raise CommandError(str(exc)) from exc

        receipts = sum((row.receipts for row in rows), Decimal("0"))
        payments = sum((row.payments for row in rows), Decimal("0"))
        result = {
            "total": {
                "row_count": len(rows),
                "page_count": page_count,
                "receipts": format(receipts, "f"),
                "payments": format(payments, "f"),
                "net_cash_flow": format(receipts - payments, "f"),
            }
        }
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
