"""Strictly read-only 1C Fresh OData cash-flow reader."""

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
    normalize_guid,
    parse_month,
    read_odata_pages,
    validate_config,
)


ENTITY_SET = "AccumulationRegister_ДвиженияДенежныхСредств_RecordType"
FIELDS = (
    "Recorder", "Recorder_Type", "LineNumber", "Period", "Active",
    "Организация_Key", "ТипДенежныхСредств", "БанковскийСчетКасса",
    "Валюта_Key", "Статья_Key", "ХозяйственнаяОперация_Key", "Проект_Key",
    "Подразделение_Key", "Аналитика", "СуммаПриход", "СуммаРасход",
)


@dataclass(frozen=True)
class CashFlowODataRow:
    recorder: str
    recorder_type: str
    line_number: int
    period: datetime
    source_date: date
    organization_guid: str
    article_guid: str | None
    receipts: Decimal
    payments: Decimal
    dimensions: dict

    @property
    def net_cash_flow(self):
        return self.receipts - self.payments

    @property
    def identity(self):
        return self.recorder, self.recorder_type, self.line_number


def _required_text(value, field, *, max_length=500):
    if not isinstance(value, str) or not value.strip():
        raise ODataPreviewError(f"{field} must be a non-empty string")
    value = value.strip()
    if len(value) > max_length:
        raise ODataPreviewError(f"{field} is too long")
    return value


def _optional_scalar(value, field, *, max_length=700):
    if value is None:
        return ""
    if isinstance(value, (dict, list, bool, float)):
        raise ODataPreviewError(f"{field} must be a string or integer")
    value = str(value).strip()
    if len(value) > max_length:
        raise ODataPreviewError(f"{field} is too long")
    return value


def _optional_guid(value, field):
    if value in (None, ""):
        return ""
    normalized = normalize_guid(value, field=field, allow_zero=True)
    return "" if normalized == ZERO_GUID else normalized


def _money(value, field):
    amount = _decimal(value, field=field)
    if amount != amount.quantize(Decimal("0.01")):
        raise ODataPreviewError(f"{field} must have at most two decimal places")
    return amount


def _build_initial_url(base_url, start, end_exclusive, organizations):
    organization_filter = " or ".join(
        f"Организация_Key eq guid'{guid}'" for guid in organizations
    )
    filters = (
        "Active eq true and "
        f"Period ge datetime'{start.isoformat()}T00:00:00' and "
        f"Period lt datetime'{end_exclusive.isoformat()}T00:00:00' and "
        f"({organization_filter})"
    )
    query = "&".join((
        f"$select={quote(','.join(FIELDS))}",
        f"$filter={quote(filters)}",
    ))
    return f"{base_url}{quote(ENTITY_SET, safe='')}?{query}"


def _parse_row(raw, start, end_exclusive, allowed_organizations):
    if not isinstance(raw, dict):
        raise ODataPreviewError("OData row must be an object")
    if raw.get("Active") is not True:
        return None
    period, calendar_date = _period(raw.get("Period"))
    if not start <= calendar_date < end_exclusive:
        raise ODataPreviewError("OData returned a row outside the requested month range")
    organization = normalize_guid(raw.get("Организация_Key"), field="Организация_Key")
    if organization not in allowed_organizations:
        raise ODataPreviewError("OData returned an organization outside the allowlist")
    line_raw = raw.get("LineNumber")
    if isinstance(line_raw, (float, bool)):
        raise ODataPreviewError("LineNumber must be a non-negative integer")
    try:
        line_number = int(line_raw)
    except (TypeError, ValueError) as exc:
        raise ODataPreviewError("LineNumber must be a non-negative integer") from exc
    if line_number < 0 or line_number > 2147483647:
        raise ODataPreviewError("LineNumber must be a non-negative integer")
    receipts = _money(raw.get("СуммаПриход"), "СуммаПриход")
    payments = _money(raw.get("СуммаРасход"), "СуммаРасход")
    if receipts < 0 or payments < 0:
        raise ODataPreviewError("Cash-flow receipts and payments must be non-negative")
    article_raw = raw.get("Статья_Key")
    if article_raw in (None, ""):
        article_guid = None
    else:
        normalized_article = normalize_guid(
            article_raw, field="Статья_Key", allow_zero=True
        )
        article_guid = None if normalized_article == ZERO_GUID else normalized_article
    dimensions = {
        "cash_type": _optional_scalar(raw.get("ТипДенежныхСредств"), "ТипДенежныхСредств"),
        "account_or_cash": _optional_scalar(raw.get("БанковскийСчетКасса"), "БанковскийСчетКасса"),
        "currency_guid": _optional_guid(raw.get("Валюта_Key"), "Валюта_Key"),
        "operation_guid": _optional_guid(raw.get("ХозяйственнаяОперация_Key"), "ХозяйственнаяОперация_Key"),
        "project_guid": _optional_guid(raw.get("Проект_Key"), "Проект_Key"),
        "department_guid": _optional_guid(raw.get("Подразделение_Key"), "Подразделение_Key"),
        "analytics": _optional_scalar(raw.get("Аналитика"), "Аналитика"),
    }
    return CashFlowODataRow(
        recorder=_required_text(raw.get("Recorder"), "Recorder"),
        recorder_type=_required_text(raw.get("Recorder_Type"), "Recorder_Type", max_length=300),
        line_number=line_number,
        period=period,
        source_date=calendar_date,
        organization_guid=organization,
        article_guid=article_guid,
        receipts=receipts,
        payments=payments,
        dimensions=dimensions,
    )


def read_cashflow_rows(
    config: ODataConfig,
    start_month: str,
    end_month: str,
    organization_guids: Iterable[str] | None = None,
    *, opener=None,
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
        raise ODataPreviewError("Requested organizations must be in the configured allowlist")
    initial_url = _build_initial_url(config.base_url, start, end_exclusive, requested)
    rows = []
    seen = set()
    page_count = 0
    for raw_rows, page_count in read_odata_pages(config, initial_url, opener=opener):
        for raw in raw_rows:
            row = _parse_row(raw, start, end_exclusive, set(requested))
            if row is None:
                continue
            if row.identity in seen:
                raise ODataPreviewError(
                    "Duplicate Recorder + Recorder_Type + LineNumber identity"
                )
            if len(rows) >= config.max_rows:
                raise ODataPreviewError("OData response exceeded the configured row limit")
            seen.add(row.identity)
            rows.append(row)
    return rows, page_count
