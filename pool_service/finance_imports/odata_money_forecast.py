"""Safe GET-only 1C reader for management money planning inputs."""
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from urllib.parse import quote
from urllib.request import build_opener

from .odata_finance_position import config_from_settings
from .odata_profit import (
    NoRedirectHandler,
    ODataPreviewError,
    ZERO_GUID,
    normalize_guid,
    read_odata_pages,
    validate_config,
)

CUSTOMER_ORDER = "Document_ЗаказПокупателя"
CUSTOMER_SCHEDULE = "Document_ЗаказПокупателя_ПлатежныйКалендарь"
CUSTOMER_PREPAYMENT = "Document_ЗаказПокупателя_Предоплата"
SUPPLIER_ORDER = "Document_ЗаказПоставщику"
SUPPLIER_SCHEDULE = "Document_ЗаказПоставщику_ПлатежныйКалендарь"
COUNTERPARTIES = "Catalog_Контрагенты"
CUSTOMER_STATES = "Catalog_СостоянияЗаказовПокупателей"

CUSTOMER_ORDER_FIELDS = (
    "Ref_Key", "Date", "DeletionMark", "Posted", "Number", "Организация_Key",
    "Контрагент_Key", "Договор_Key", "Ответственный_Key", "СостояниеЗаказа",
    "СостояниеЗаказа_Type", "ДатаИзменения", "ДатаОтгрузки",
    "ЗапланироватьОплату", "ОплатаДо", "СуммаДокумента", "ПричинаОтмены_Key",
)
SUPPLIER_ORDER_FIELDS = (
    "Ref_Key", "Date", "DeletionMark", "Posted", "Number", "Организация_Key",
    "Контрагент_Key", "Договор_Key", "Ответственный_Key", "СостояниеЗаказа_Key",
    "ДатаПоступления", "ЗапланироватьОплату", "СуммаДокумента",
)
SCHEDULE_FIELDS = (
    "Ref_Key", "LineNumber", "ДатаОплаты", "СуммаОплаты", "ПроцентОплаты",
)
PREPAYMENT_FIELDS = (
    "Ref_Key", "LineNumber", "Документ", "Документ_Type", "СуммаПлатежа",
    "СуммаРасчетов", "ЭтоПредоплатаБезЗаказа",
)

INCLUDED_CUSTOMER_STATES = frozenset({"В работе", "На выполнении", "Завершен"})
POSSIBLE_CUSTOMER_STATES = frozenset({"Не обработан", "Выставлен счет"})


@dataclass(frozen=True)
class MoneyForecastSourceRow:
    direction: str
    item_kind: str
    source_identity: str
    order_guid: str | None
    agreement_guid: str | None
    counterparty_guid: str | None
    counterparty_name: str
    order_number: str
    order_date: date | None
    order_state: str
    expected_amount: Decimal | None
    matched_paid_amount: Decimal | None
    remaining_amount: Decimal | None
    payment_match_status: str
    contractual_due_date: date | None
    expected_date: date | None
    expected_month: date | None
    date_precision: str
    basis: str
    confirmation_status: str
    source_updated_at: datetime | None
    source_payload: dict


@dataclass(frozen=True)
class MoneyForecastSourceSnapshot:
    source_at: datetime
    fetched_at: datetime
    rows: tuple[MoneyForecastSourceRow, ...]
    diagnostics: dict


class MoneyForecastReadError(ODataPreviewError):
    pass


def _organization_filter(config):
    return " or ".join(
        f"Организация_Key eq guid'{guid}'" for guid in config.organization_guids
    )


def _entity_url(config, entity, fields, *, filters=None):
    allowed = {
        CUSTOMER_ORDER, CUSTOMER_SCHEDULE, CUSTOMER_PREPAYMENT,
        SUPPLIER_ORDER, SUPPLIER_SCHEDULE, COUNTERPARTIES, CUSTOMER_STATES,
    }
    if entity not in allowed:
        raise MoneyForecastReadError("1C planning entity is not allowed")
    parts = [f"$select={quote(','.join(fields))}"]
    if filters:
        parts.append(f"$filter={quote(filters)}")
    return f"{config.base_url}{quote(entity, safe='')}?{'&'.join(parts)}"


def _read(config, entity, fields, *, filters=None, opener=None):
    rows = []
    url = _entity_url(config, entity, fields, filters=filters)
    for page_rows, _ in read_odata_pages(config, url, opener=opener):
        rows.extend(page_rows)
        if len(rows) > config.max_rows:
            raise MoneyForecastReadError("1C planning rows exceeded configured limit")
    return rows


def _guid(value, field, optional=False):
    if optional and value in (None, "", ZERO_GUID):
        return None
    return normalize_guid(value, field=field)


def _decimal(value, field, optional=False):
    if optional and value in (None, ""):
        return None
    if isinstance(value, (bool, float)) or value is None:
        raise MoneyForecastReadError(f"{field} must be decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise MoneyForecastReadError(f"{field} must be decimal") from exc
    if not result.is_finite():
        raise MoneyForecastReadError(f"{field} must be finite")
    return result.quantize(Decimal("0.01"))


def _date(value):
    if not value or str(value).startswith("0001-01-01"):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise MoneyForecastReadError("1C planning date is invalid") from exc


def _datetime(value):
    if not value or str(value).startswith("0001-01-01"):
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise MoneyForecastReadError("1C planning datetime is invalid") from exc


def _text(value, limit=300):
    if value is None:
        return ""
    value = str(value).strip()
    return value[:limit]


def _identity(*parts):
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _read_for_refs(config, entity, fields, refs, opener):
    refs = sorted(set(refs))
    rows = []
    for start in range(0, len(refs), 40):
        chunk = refs[start:start + 40]
        expr = " or ".join(f"Ref_Key eq guid'{g}'" for g in chunk)
        rows.extend(_read(config, entity, fields, filters=expr, opener=opener))
    return rows


def _lookup_descriptions(config, entity, guids, opener):
    values = sorted({g for g in guids if g})
    if not values:
        return {}
    result = {}
    for start in range(0, len(values), 40):
        chunk = values[start:start + 40]
        expr = " or ".join(f"Ref_Key eq guid'{g}'" for g in chunk)
        rows = _read(
            config, entity, ("Ref_Key", "Description", "DeletionMark"),
            filters=expr, opener=opener,
        )
        for raw in rows:
            key = _guid(raw.get("Ref_Key"), "Ref_Key")
            if raw.get("DeletionMark") is True:
                continue
            result[key] = _text(raw.get("Description"))
    return result


def _customer_confirmation(state):
    if state in INCLUDED_CUSTOMER_STATES:
        return "confirmed"
    if state in POSSIBLE_CUSTOMER_STATES:
        return "possible"
    return "review"


def read_money_forecast(now, *, config=None, opener=None):
    """Read only planning inputs; no page/request path should call this function."""
    config = validate_config(config or config_from_settings())
    client = opener or build_opener(NoRedirectHandler())
    org_filter = _organization_filter(config)

    history_start = date(max(1, now.date().year - 2), 1, 1)
    date_filter = f"Date ge datetime'{history_start.isoformat()}T00:00:00'"
    customer_orders_raw = _read(
        config, CUSTOMER_ORDER, CUSTOMER_ORDER_FIELDS,
        filters=f"Posted eq true and DeletionMark eq false and {date_filter} and ({org_filter})",
        opener=client,
    )
    supplier_orders_raw = _read(
        config, SUPPLIER_ORDER, SUPPLIER_ORDER_FIELDS,
        filters=f"Posted eq true and DeletionMark eq false and {date_filter} and ({org_filter})",
        opener=client,
    )
    customer_order_refs = {
        _guid(r.get("Ref_Key"), "Ref_Key") for r in customer_orders_raw
    }
    supplier_order_refs = {
        _guid(r.get("Ref_Key"), "Ref_Key") for r in supplier_orders_raw
    }

    customer_schedule = _read_for_refs(
        config, CUSTOMER_SCHEDULE, SCHEDULE_FIELDS, customer_order_refs, client
    )
    customer_prepayment = _read_for_refs(
        config, CUSTOMER_PREPAYMENT, PREPAYMENT_FIELDS, customer_order_refs, client
    )
    supplier_schedule = _read_for_refs(
        config, SUPPLIER_SCHEDULE, SCHEDULE_FIELDS, supplier_order_refs, client
    )

    counterparty_guids = {
        _guid(r.get("Контрагент_Key"), "Контрагент_Key", optional=True)
        for r in customer_orders_raw + supplier_orders_raw
    }
    state_guids = {
        _guid(r.get("СостояниеЗаказа"), "СостояниеЗаказа", optional=True)
        for r in customer_orders_raw
    }
    counterparties = _lookup_descriptions(
        config, COUNTERPARTIES, counterparty_guids, client
    )
    states = _lookup_descriptions(config, CUSTOMER_STATES, state_guids, client)

    customer_schedule_by_order = {}
    for raw in customer_schedule:
        ref = _guid(raw.get("Ref_Key"), "Ref_Key")
        customer_schedule_by_order.setdefault(ref, []).append(raw)
    prepayment_by_order = {}
    for raw in customer_prepayment:
        ref = _guid(raw.get("Ref_Key"), "Ref_Key")
        amount = _decimal(raw.get("СуммаРасчетов"), "СуммаРасчетов", optional=True)
        if amount is None:
            amount = _decimal(raw.get("СуммаПлатежа"), "СуммаПлатежа", optional=True)
        if amount is not None:
            prepayment_by_order[ref] = prepayment_by_order.get(ref, Decimal("0.00")) + amount
    supplier_schedule_by_order = {}
    for raw in supplier_schedule:
        ref = _guid(raw.get("Ref_Key"), "Ref_Key")
        supplier_schedule_by_order.setdefault(ref, []).append(raw)

    rows = []
    review_count = 0
    possible_count = 0

    for raw in customer_orders_raw:
        ref = _guid(raw.get("Ref_Key"), "Ref_Key")
        party = _guid(raw.get("Контрагент_Key"), "Контрагент_Key", optional=True)
        agreement = _guid(raw.get("Договор_Key"), "Договор_Key", optional=True)
        state_guid = _guid(raw.get("СостояниеЗаказа"), "СостояниеЗаказа", optional=True)
        state = states.get(state_guid, "")
        confirmation = _customer_confirmation(state)
        if confirmation == "possible":
            possible_count += 1
        total = _decimal(raw.get("СуммаДокумента"), "СуммаДокумента")
        prepaid = prepayment_by_order.get(ref, Decimal("0.00"))
        remaining_order = max(total - prepaid, Decimal("0.00"))
        due = _date(raw.get("ОплатаДо"))
        schedule = sorted(
            customer_schedule_by_order.get(ref, []),
            key=lambda x: (x.get("ДатаОплаты") or "", int(x.get("LineNumber") or 0)),
        )

        if schedule:
            multi_with_prepayment = len(schedule) > 1 and prepaid != 0
            for item in schedule:
                amount = _decimal(item.get("СуммаОплаты"), "СуммаОплаты")
                pay_date = _date(item.get("ДатаОплаты"))
                if not pay_date:
                    remaining = None
                    match_status = "missing_date"
                    row_confirmation = "review"
                elif multi_with_prepayment:
                    remaining = None
                    match_status = "ambiguous_prepayment_allocation"
                    row_confirmation = "review"
                else:
                    matched = min(prepaid, amount) if len(schedule) == 1 else Decimal("0.00")
                    remaining = max(amount - matched, Decimal("0.00"))
                    match_status = "order_prepayment_matched" if matched else "no_prepayment"
                    row_confirmation = confirmation
                if row_confirmation == "review":
                    review_count += 1
                rows.append(MoneyForecastSourceRow(
                    direction="receipt",
                    item_kind="order_schedule",
                    source_identity=_identity("customer_schedule", ref, item.get("LineNumber")),
                    order_guid=ref,
                    agreement_guid=agreement,
                    counterparty_guid=party,
                    counterparty_name=counterparties.get(party, ""),
                    order_number=_text(raw.get("Number"), 80),
                    order_date=_date(raw.get("Date")),
                    order_state=state,
                    expected_amount=amount,
                    matched_paid_amount=(amount - remaining) if remaining is not None else None,
                    remaining_amount=remaining,
                    payment_match_status=match_status,
                    contractual_due_date=pay_date,
                    expected_date=pay_date,
                    expected_month=pay_date.replace(day=1) if pay_date else None,
                    date_precision="exact" if pay_date else "unknown",
                    basis="График оплаты заказа в 1С",
                    confirmation_status=row_confirmation,
                    source_updated_at=_datetime(raw.get("ДатаИзменения")),
                    source_payload={"order_amount": str(total), "order_prepayment": str(prepaid)},
                ))
            continue

        if due:
            rows.append(MoneyForecastSourceRow(
                direction="receipt",
                item_kind="order_due",
                source_identity=_identity("customer_due", ref),
                order_guid=ref,
                agreement_guid=agreement,
                counterparty_guid=party,
                counterparty_name=counterparties.get(party, ""),
                order_number=_text(raw.get("Number"), 80),
                order_date=_date(raw.get("Date")),
                order_state=state,
                expected_amount=total,
                matched_paid_amount=prepaid,
                remaining_amount=remaining_order,
                payment_match_status="order_prepayment_matched" if prepaid else "no_prepayment",
                contractual_due_date=due,
                expected_date=due,
                expected_month=due.replace(day=1),
                date_precision="exact",
                basis="Срок ОплатаДо заказа в 1С",
                confirmation_status=confirmation,
                source_updated_at=_datetime(raw.get("ДатаИзменения")),
                source_payload={"order_amount": str(total), "order_prepayment": str(prepaid)},
            ))
            continue

        # A live agreed order without a schedule/due date is visible, but the
        # document amount is intentionally not treated as an unpaid forecast.
        if confirmation == "confirmed":
            review_count += 1
            rows.append(MoneyForecastSourceRow(
                direction="receipt",
                item_kind="order_due",
                source_identity=_identity("customer_unknown_due", ref),
                order_guid=ref,
                agreement_guid=agreement,
                counterparty_guid=party,
                counterparty_name=counterparties.get(party, ""),
                order_number=_text(raw.get("Number"), 80),
                order_date=_date(raw.get("Date")),
                order_state=state,
                expected_amount=None,
                matched_paid_amount=prepaid if prepaid else None,
                remaining_amount=None,
                payment_match_status="amount_not_verified",
                contractual_due_date=None,
                expected_date=None,
                expected_month=None,
                date_precision="unknown",
                basis="Согласованный заказ без графика и срока оплаты",
                confirmation_status="review",
                source_updated_at=_datetime(raw.get("ДатаИзменения")),
                source_payload={"document_amount": str(total), "order_prepayment": str(prepaid)},
            ))

    for raw in supplier_orders_raw:
        ref = _guid(raw.get("Ref_Key"), "Ref_Key")
        party = _guid(raw.get("Контрагент_Key"), "Контрагент_Key", optional=True)
        agreement = _guid(raw.get("Договор_Key"), "Договор_Key", optional=True)
        schedule = supplier_schedule_by_order.get(ref, [])
        for item in schedule:
            amount = _decimal(item.get("СуммаОплаты"), "СуммаОплаты")
            pay_date = _date(item.get("ДатаОплаты"))
            row_confirmation = "confirmed" if pay_date else "review"
            if row_confirmation == "review":
                review_count += 1
            rows.append(MoneyForecastSourceRow(
                direction="payment",
                item_kind="supplier_schedule",
                source_identity=_identity("supplier_schedule", ref, item.get("LineNumber")),
                order_guid=ref,
                agreement_guid=agreement,
                counterparty_guid=party,
                counterparty_name=counterparties.get(party, ""),
                order_number=_text(raw.get("Number"), 80),
                order_date=_date(raw.get("Date")),
                order_state="",
                expected_amount=amount,
                matched_paid_amount=None,
                remaining_amount=amount if pay_date else None,
                payment_match_status="not_checked",
                contractual_due_date=pay_date,
                expected_date=pay_date,
                expected_month=pay_date.replace(day=1) if pay_date else None,
                date_precision="exact" if pay_date else "unknown",
                basis="График оплаты заказа поставщику в 1С",
                confirmation_status=row_confirmation,
                source_updated_at=None,
                source_payload={},
            ))

    return MoneyForecastSourceSnapshot(
        source_at=now,
        fetched_at=now,
        rows=tuple(rows),
        diagnostics={
            "customer_orders": len(customer_orders_raw),
            "customer_schedule_rows": len(customer_schedule),
            "customer_prepayment_rows": len(customer_prepayment),
            "supplier_orders": len(supplier_orders_raw),
            "supplier_schedule_rows": len(supplier_schedule),
            "review_rows": review_count,
            "possible_orders": possible_count,
            "payment_coverage_complete": False,
        },
    )
