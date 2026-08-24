"""Strictly read-only 1C Fresh OData gross-profit preview."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import posixpath
import re
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit, urlunsplit, unquote
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID


ODATA_BASE_PATH = "/odata/standard.odata/"
ENTITY_SET = "AccumulationRegister_Продажи_RecordType"
FIELDS = (
    "Recorder", "LineNumber", "Period", "Active", "Организация_Key",
    "Номенклатура_Key", "Контрагент_Key", "Ответственный_Key", "Документ", "Количество", "Сумма",
    "СуммаНДС", "Себестоимость",
)
ZERO_GUID = "00000000-0000-0000-0000-000000000000"
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


class ODataPreviewError(Exception):
    """Safe user-facing configuration, transport, or payload error."""


class NoRedirectHandler(HTTPRedirectHandler):
    """Prevent urllib from following HTTP redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class ODataConfig:
    base_url: str
    username: str = ""
    password: str = ""
    organization_guids: tuple[str, ...] = ()
    timeout_seconds: float = 15
    max_pages: int = 100
    max_rows: int = 100000


@dataclass(frozen=True)
class ProfitRow:
    recorder: str
    line_number: int
    period: datetime
    source_date: date
    organization_guid: str
    nomenclature_guid: str
    customer_guid: str
    responsible_guid: str
    document: str
    quantity: Decimal
    revenue: Decimal
    vat: Decimal
    cost: Decimal

    @property
    def identity(self):
        return self.recorder, self.line_number


def parse_month(value: str) -> date:
    if not isinstance(value, str) or not MONTH_RE.fullmatch(value):
        raise ODataPreviewError("Month must use YYYY-MM format")
    try:
        return date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise ODataPreviewError("Month must be a valid calendar month") from exc


def next_month(value: date) -> date:
    return date(value.year + (value.month == 12), value.month % 12 + 1, 1)


def normalize_guid(value, *, field: str, allow_zero: bool = False) -> str:
    try:
        normalized = str(UUID(str(value))).lower()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ODataPreviewError(f"{field} must be a GUID") from exc
    if not allow_zero and normalized == ZERO_GUID:
        raise ODataPreviewError(f"{field} must not be the zero GUID")
    return normalized


def _effective_port(parts):
    try:
        return parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise ODataPreviewError("URL contains an invalid port") from exc


def _decoded_path(path: str) -> str:
    decoded = path
    for _ in range(5):
        updated = unquote(decoded)
        if updated == decoded:
            break
        decoded = updated
    if "\\" in decoded or "\x00" in decoded:
        raise ODataPreviewError("URL path contains forbidden characters")
    segments = decoded.split("/")
    if any(segment in (".", "..") for segment in segments):
        raise ODataPreviewError("URL path contains dot-segments")
    normalized = posixpath.normpath(decoded)
    if decoded.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def validate_config(config: ODataConfig) -> ODataConfig:
    parts = urlsplit(config.base_url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ODataPreviewError("OData base URL must be an absolute HTTP(S) URL")
    if parts.username is not None or parts.password is not None:
        raise ODataPreviewError("OData base URL must not contain credentials")
    if parts.query or parts.fragment:
        raise ODataPreviewError("OData base URL must not contain query or fragment")
    if not config.base_url.endswith(ODATA_BASE_PATH):
        raise ODataPreviewError(f"OData base URL must end with {ODATA_BASE_PATH}")
    if not _decoded_path(parts.path).endswith(ODATA_BASE_PATH):
        raise ODataPreviewError("OData base URL has an invalid encoded path")
    if bool(config.username) != bool(config.password):
        raise ODataPreviewError("OData username and password must be set together")
    try:
        timeout = float(config.timeout_seconds)
    except (TypeError, ValueError) as exc:
        raise ODataPreviewError("OData timeout must be a number") from exc
    if not 0 < timeout <= 120:
        raise ODataPreviewError("OData timeout must be between 0 and 120 seconds")
    try:
        max_pages = int(config.max_pages)
    except (TypeError, ValueError) as exc:
        raise ODataPreviewError("OData max pages must be an integer") from exc
    if not 1 <= max_pages <= 10000:
        raise ODataPreviewError("OData max pages must be between 1 and 10000")
    try:
        max_rows = int(config.max_rows)
    except (TypeError, ValueError) as exc:
        raise ODataPreviewError("OData max rows must be an integer") from exc
    if not 1 <= max_rows <= 1000000:
        raise ODataPreviewError("OData max rows must be between 1 and 1000000")
    organizations = tuple(dict.fromkeys(
        normalize_guid(value, field="Organization allowlist GUID")
        for value in config.organization_guids
    ))
    if not organizations:
        raise ODataPreviewError("At least one organization GUID must be configured")
    try:
        explicit_port = parts.port
    except ValueError as exc:
        raise ODataPreviewError("OData base URL contains an invalid port") from exc
    canonical_url = urlunsplit((
        parts.scheme.lower(),
        parts.hostname.lower() + (f":{explicit_port}" if explicit_port else ""),
        parts.path,
        "",
        "",
    ))
    return ODataConfig(
        canonical_url, config.username, config.password, organizations,
        timeout, max_pages, max_rows,
    )


def _safe_next_url(base_url: str, current_url: str, next_link: str) -> str:
    if not isinstance(next_link, str) or not next_link:
        raise ODataPreviewError("OData nextLink must be a non-empty string")
    candidate = urljoin(current_url, next_link)
    base = urlsplit(base_url)
    parts = urlsplit(candidate)
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ODataPreviewError("OData nextLink contains forbidden URL components")
    if (
        parts.scheme.lower() != base.scheme.lower()
        or (parts.hostname or "").lower() != (base.hostname or "").lower()
        or _effective_port(parts) != _effective_port(base)
    ):
        raise ODataPreviewError("OData nextLink must stay on the configured origin")
    base_path = _decoded_path(base.path)
    candidate_path = _decoded_path(parts.path)
    if not candidate_path.startswith(base_path):
        raise ODataPreviewError("OData nextLink must stay inside the configured base path")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))


def _decimal(value, *, field: str) -> Decimal:
    if isinstance(value, float):
        raise ODataPreviewError(f"{field} must not be a JSON float")
    if isinstance(value, bool) or value is None:
        raise ODataPreviewError(f"{field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ODataPreviewError(f"{field} must be decimal") from exc
    if not parsed.is_finite():
        raise ODataPreviewError(f"{field} must be a finite decimal")
    return parsed


def _period(value) -> tuple[datetime, date]:
    if not isinstance(value, str):
        raise ODataPreviewError("Period must be an ISO datetime string")
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ODataPreviewError("Period must be an ISO datetime string") from exc
    calendar_date = parsed.date()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc), calendar_date


def _build_initial_url(base_url: str, start: date, end_exclusive: date, organizations) -> str:
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


def _payload_page(raw: bytes):
    try:
        payload = json.loads(raw.decode("utf-8-sig"), parse_float=Decimal)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ODataPreviewError("OData returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ODataPreviewError("OData response must be an object")
    if "d" in payload:
        legacy = payload["d"]
        if not isinstance(legacy, dict) or not isinstance(legacy.get("results", []), list):
            raise ODataPreviewError("OData legacy response has invalid shape")
        return legacy.get("results", []), legacy.get("__next")
    rows = payload.get("value")
    if not isinstance(rows, list):
        raise ODataPreviewError("OData response value must be a list")
    return rows, payload.get("@odata.nextLink") or payload.get("odata.nextLink")


def read_odata_pages(config: ODataConfig, initial_url: str, *, opener=None):
    """Read a bounded same-origin OData result using GET without redirects."""
    config = validate_config(config)
    current_url = _safe_next_url(config.base_url, config.base_url, initial_url)
    client = opener or build_opener(NoRedirectHandler())
    seen_urls = set()
    page_count = 0
    while current_url:
        if current_url in seen_urls:
            raise ODataPreviewError("OData pagination loop detected")
        if page_count >= config.max_pages:
            raise ODataPreviewError("OData pagination exceeded the configured page limit")
        seen_urls.add(current_url)
        page_count += 1
        request = Request(current_url, method="GET")
        request.add_header("Accept", "application/json")
        if config.username:
            token = base64.b64encode(
                f"{config.username}:{config.password}".encode("utf-8")
            ).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with client.open(request, timeout=config.timeout_seconds) as response:
                raw_rows, next_link = _payload_page(response.read())
        except HTTPError as exc:
            raise ODataPreviewError(f"OData HTTP error {exc.code}") from exc
        except URLError as exc:
            raise ODataPreviewError("OData request failed") from exc
        yield raw_rows, page_count
        current_url = (
            _safe_next_url(config.base_url, current_url, next_link)
            if next_link else None
        )


def _parse_row(raw, start: date, end_exclusive: date, allowed_organizations) -> ProfitRow | None:
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
    try:
        line_number = int(raw.get("LineNumber"))
    except (TypeError, ValueError) as exc:
        raise ODataPreviewError("LineNumber must be an integer") from exc
    if isinstance(raw.get("LineNumber"), (float, bool)) or line_number < 0:
        raise ODataPreviewError("LineNumber must be a non-negative integer")
    document = raw.get("Документ")
    if document is not None and not isinstance(document, str):
        raise ODataPreviewError("Документ must be a string")
    return ProfitRow(
        recorder=normalize_guid(raw.get("Recorder"), field="Recorder"),
        line_number=line_number,
        period=period,
        source_date=calendar_date,
        organization_guid=organization,
        nomenclature_guid=normalize_guid(raw.get("Номенклатура_Key"), field="Номенклатура_Key"),
        customer_guid=normalize_guid(
            raw.get("Контрагент_Key"), field="Контрагент_Key", allow_zero=True
        ),
        responsible_guid=normalize_guid(
            raw.get("Ответственный_Key"), field="Ответственный_Key"
        ),
        document=(document or "").strip()[:500],
        quantity=_decimal(raw.get("Количество"), field="Количество"),
        revenue=_decimal(raw.get("Сумма"), field="Сумма"),
        vat=_decimal(raw.get("СуммаНДС"), field="СуммаНДС"),
        cost=_decimal(raw.get("Себестоимость"), field="Себестоимость"),
    )


def read_profit_preview(
    config: ODataConfig,
    start_month: str,
    end_month: str,
    organization_guids: Iterable[str] | None = None,
    *,
    opener=None,
):
    rows, page_count = read_profit_rows(
        config, start_month, end_month, organization_guids, opener=opener
    )
    return summarize_profit(rows, page_count)


def read_profit_rows(
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
        raise ODataPreviewError("Requested organizations must be in the configured allowlist")
    initial_url = _build_initial_url(config.base_url, start, end_exclusive, requested)
    seen_rows = set()
    rows = []
    page_count = 0
    for raw_rows, page_count in read_odata_pages(config, initial_url, opener=opener):
        for raw_row in raw_rows:
            row = _parse_row(raw_row, start, end_exclusive, set(requested))
            if row is None:
                continue
            if row.identity in seen_rows:
                raise ODataPreviewError("Duplicate Recorder + LineNumber identity")
            if len(rows) >= config.max_rows:
                raise ODataPreviewError("OData response exceeded the configured row limit")
            seen_rows.add(row.identity)
            rows.append(row)
    return rows, page_count


def summarize_profit(rows: Iterable[ProfitRow], page_count: int):
    rows = list(rows)
    by_organization = {}
    for row in rows:
        totals = by_organization.setdefault(row.organization_guid, {
            "row_count": 0, "quantity": Decimal("0"), "revenue": Decimal("0"),
            "vat": Decimal("0"), "cost": Decimal("0"), "gross_profit": Decimal("0"),
        })
        totals["row_count"] += 1
        totals["quantity"] += row.quantity
        totals["revenue"] += row.revenue
        totals["vat"] += row.vat
        totals["cost"] += row.cost
        totals["gross_profit"] += row.revenue - row.cost
    total = {
        "row_count": len(rows),
        "page_count": page_count,
        "quantity": sum((row.quantity for row in rows), Decimal("0")),
        "revenue": sum((row.revenue for row in rows), Decimal("0")),
        "vat": sum((row.vat for row in rows), Decimal("0")),
        "cost": sum((row.cost for row in rows), Decimal("0")),
        "gross_profit": sum((row.revenue - row.cost for row in rows), Decimal("0")),
    }
    return {"total": total, "organizations": by_organization}
