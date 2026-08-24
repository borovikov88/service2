import json
from decimal import Decimal

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pool_service.finance_imports.odata_profit import (
    ODataConfig,
    ODataPreviewError,
    read_profit_preview,
)


class Command(BaseCommand):
    help = "Read-only preview of 1C Fresh OData gross-profit aggregates"

    def add_arguments(self, parser):
        parser.add_argument("--start-month", required=True)
        parser.add_argument("--end-month", required=True)
        parser.add_argument("--organization-guid", action="append", dest="organization_guids")

    def handle(self, *args, **options):
        config = ODataConfig(
            base_url=settings.ONEC_ODATA_BASE_URL,
            username=settings.ONEC_ODATA_USERNAME,
            password=settings.ONEC_ODATA_PASSWORD,
            organization_guids=tuple(settings.ONEC_ODATA_ORGANIZATION_GUIDS),
            timeout_seconds=settings.ONEC_ODATA_TIMEOUT_SECONDS,
            max_pages=settings.ONEC_ODATA_MAX_PAGES,
        )
        try:
            result = read_profit_preview(
                config,
                options["start_month"],
                options["end_month"],
                options["organization_guids"],
            )
        except ODataPreviewError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, default=_json_value, sort_keys=True))


def _json_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"Cannot serialize {type(value).__name__}")
