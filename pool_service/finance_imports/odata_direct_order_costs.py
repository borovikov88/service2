"""Strict read-only reader for direct customer-order expenses from 1C."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable
from urllib.parse import quote

from .odata_profit import (
    ODataConfig,
    ODataPreviewError,
    ZERO_GUID,
    _decimal,
    _period,
    next_month,
    normalize_document_type,
    normalize_guid,
    parse_month,
    read_odata_pages,
    validate_config,
)


ENTITY_SET = "AccumulationRegister_ДоходыИРасходы_RecordType"
RECORDER_TYPE = "Document_ПриходнаяНакладная"
RECORDER_TYPE_ODATA = f"StandardODATA.{RECORDER_TYPE}"
FIELDS = (
    "Recorder",
    "Recorder_Type",
    "LineNumber",
    "Period",
    "Active",
    "Организация_Key",
    "ЗаказПокупателя_Key",
    "СодержаниеПроводки",
    "СуммаРасходов",
    "СчетУчета_Key",
    "ХозяйственнаяОперация_Key",
)


@dataclass(frozen=True)
class DirectOrderExpenseRow:
    recorder: str
    recorder_type: str
    line_number: int
    period: datetime
    source_period: str
    source_date: date
    organization_guid: str
    order_guid: str
    content: str
    amount: Decimal
    account_guid: str | None
    operation_guid: str | None

    @property
    def identity(self):
        return self.recorder, self.line_number


def _optional_guid(value, *, field: str) -> str | None:
    if value in (None, "", ZERO_GUID):
        return None
    return normalize_guid(value, field=field)


def _build_initial_url(base_url: str, start: date, end_exclusive: date, organizations) -> str:
    organization_filter = " or ".join(
        f"Организация_Key eq guid'{guid}'" for guid in organizations
    )
    filters = (
        "Active eq true and "
        f"Period ge datetime'{start.isoformat()}T00:00:00' and "
        f"Period lt datetime'{end_exclusive.isoformat()}T00:00:00' and "
        f"({organization_filter}) and "
        f"ЗаказПокупателя_Key ne guid'{ZERO_GUID}' and "
        f"Recorder_Type eq '{RECORDER_TYPE_ODATA}' and "
        "СуммаРасходов ne 0"
    )
    query = "&".join((
        f"$select={quote(','.join(FIELDS))}",
        f"$filter={quote(filters)}",
    ))
    return f"{base_url}{quote(ENTITY_SET, safe='')}?{query}"


def _parse_row(raw, start: date, end_exclusive: date, allowed_organizations):
    if not isinstance(raw, dict):
        raise ODataPreviewError("Direct expense OData row must be an object")
    if raw.get("Active") is not True:
        return None

    period, calendar_date = _period(raw.get("Period"))
    if not start <= calendar_date < end_exclusive:
        raise ODataPreviewError(
            "Direct expense OData returned a row outside the requested month range"
        )

    organization = normalize_guid(
        raw.get("Организация_Key"), field="Организация_Key"
    )
    if organization not in allowed_organizations:
        raise ODataPreviewError(
            "Direct expense OData returned an organization outside the allowlist"
        )

    try:
        line_number = int(raw.get("LineNumber"))
    except (TypeError, ValueError) as exc:
        raise ODataPreviewError("Direct expense LineNumber must be an integer") from exc
    if isinstance(raw.get("LineNumber"), (float, bool)) or line_number < 0:
        raise ODataPreviewError(
            "Direct expense LineNumber must be a non-negative integer"
        )

    recorder_type = normalize_document_type(
        raw.get("Recorder_Type"),
        field="Direct expense Recorder_Type",
        allowed_types={RECORDER_TYPE},
    )
    order_guid = normalize_guid(
        raw.get("ЗаказПокупателя_Key"), field="ЗаказПокупателя_Key"
    )
    amount = _decimal(raw.get("СуммаРасходов"), field="СуммаРасходов")
    if amount == 0:
        return None

    content = raw.get("СодержаниеПроводки")
    if not isinstance(content, str) or len(content) > 500:
        raise ODataPreviewError(
            "Direct expense СодержаниеПроводки must be a bounded string"
        )

    return DirectOrderExpenseRow(
        recorder=normalize_guid(raw.get("Recorder"), field="Recorder"),
        recorder_type=recorder_type,
        line_number=line_number,
        period=period,
        source_period=raw["Period"],
        source_date=calendar_date,
        organization_guid=organization,
        order_guid=order_guid,
        content=content,
        amount=amount,
        account_guid=_optional_guid(raw.get("СчетУчета_Key"), field="СчетУчета_Key"),
        operation_guid=_optional_guid(
            raw.get("ХозяйственнаяОперация_Key"),
            field="ХозяйственнаяОперация_Key",
        ),
    )


def read_direct_order_expense_rows(
    config: ODataConfig,
    start_month: str,
    end_month: str,
    organization_guids: Iterable[str] | None = None,
    *,
    opener=None,
):
    config = validate_config(config)
    start = parse_month(start_month)
    end = parse_month(end_month)
    if end < start:
        raise ODataPreviewError("End month must not precede start month")
    end_exclusive = next_month(end)
    requested = tuple(dict.fromkeys(
        normalize_guid(value, field="Requested organization GUID")
        for value in (organization_guids or config.organization_guids)
    ))
    if not requested or not set(requested).issubset(config.organization_guids):
        raise ODataPreviewError(
            "Requested organizations must be in the configured allowlist"
        )

    initial_url = _build_initial_url(
        config.base_url, start, end_exclusive, requested
    )
    rows = []
    seen_rows = set()
    page_count = 0
    allowed = set(requested)
    for raw_rows, page_count in read_odata_pages(
        config, initial_url, opener=opener
    ):
        for raw_row in raw_rows:
            row = _parse_row(raw_row, start, end_exclusive, allowed)
            if row is None:
                continue
            if row.identity in seen_rows:
                raise ODataPreviewError(
                    "Duplicate direct expense Recorder + LineNumber identity"
                )
            if len(rows) >= config.max_rows:
                raise ODataPreviewError(
                    "Direct expense OData response exceeded the configured row limit"
                )
            seen_rows.add(row.identity)
            rows.append(row)
    return rows, page_count
