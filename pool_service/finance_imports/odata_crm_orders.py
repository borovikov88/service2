"""Bounded GET-only order evidence, independent of forecasts and AR balances.

Only an explicitly identified cash receipt settling an invoice is counted as
payment. Unknown settlement operations fail closed. The two complete reads
detect observed changes; they are not a distributed transaction with 1C.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
from urllib.parse import quote, urlsplit
from urllib.request import build_opener
from django.core.exceptions import ValidationError

from .odata_finance_position import calendar_timezone, config_from_settings
from .odata_profit import (
    NoRedirectHandler, ODataPreviewError, ZERO_GUID, normalize_document_type,
    normalize_guid, read_odata_pages, validate_config,
)


ORDER = "Document_ЗаказПокупателя"
INVOICE = "Document_РасходнаяНакладная"
SETTLEMENTS = "AccumulationRegister_РасчетыСПокупателями_RecordType"
RECEIPTS = frozenset({"Document_ПоступлениеНаСчет", "Document_ПоступлениеВКассу"})
COMMON_FIELDS = ("Ref_Key", "Date", "Posted", "DeletionMark", "Number", "DataVersion",
                 "Организация_Key", "Контрагент_Key", "СуммаДокумента")
ORDER_FIELDS = COMMON_FIELDS + ("Договор_Key", "ВалютаДокумента_Key", "ПричинаОтмены_Key")
INVOICE_FIELDS = COMMON_FIELDS + ("Договор_Key", "ВалютаДокумента_Key", "Заказ", "Заказ_Type", "ВидОперации")
PAYMENT_FIELDS = COMMON_FIELDS + ("ВалютаДенежныхСредств_Key", "ВидОперации")
MOVEMENT_FIELDS = ("Recorder", "Recorder_Type", "LineNumber", "Period", "Active", "RecordType",
    "Организация_Key", "Контрагент_Key", "Договор_Key", "Заказ", "Заказ_Type", "ТипРасчетов",
    "Документ", "Документ_Type", "ДокументОплаты", "ДокументОплаты_Type", "СуммаВал", "СуммаПлатежа", "ЭтоОплатаЗаказа")
MAX_ORDERS = 500
DISCOVERY_LIMIT = 200
CHUNK = 30


class CrmOrdersReadError(ODataPreviewError):
    pass


@dataclass(frozen=True)
class CrmOrderEvidence:
    order_guid: str
    organization_guid: str
    customer_guid: str
    agreement_guid: str | None
    currency_guid: str | None
    number: str
    order_date: datetime
    amount: Decimal
    realized_amount: Decimal | None
    paid_amount: Decimal | None
    realization_complete: bool
    payment_complete: bool
    order_valid: bool
    cancelled: bool
    blockers: tuple[str, ...]
    documents: tuple[dict, ...]
    fingerprint: str
    currency_code: str = ""


@dataclass(frozen=True)
class CrmOrdersSnapshot:
    source_at: datetime
    fetched_at: datetime
    source_id: str
    rows: tuple[CrmOrderEvidence, ...]
    diagnostics: dict
    full_scope: bool = True
    requested_order_guids: tuple[str, ...] = ()


def source_identity(config=None):
    config = validate_config(config or config_from_settings())
    return hashlib.sha256(config.base_url.encode()).hexdigest()


def _currency_codes(config):
    """Reuse the existing explicitly acknowledged source/currency binding only."""
    from .odata_payroll_drafts import config_from_settings as payroll_config, _scope, auto_coverage_config
    try:
        configured = validate_config(config_from_settings())
        if source_identity(configured) != source_identity(config) or set(configured.organization_guids) != set(config.organization_guids):
            return {}
        values = payroll_config()
        organizations, currency, _ = _scope(values)
        auto_coverage_config()
        if set(organizations) != set(config.organization_guids):
            return {}
        return {currency: values["ONEC_ODATA_PAYROLL_CURRENCY_CODE"]}
    except (ValidationError, ODataPreviewError, AttributeError, TypeError):
        return {}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), default=str).encode()).hexdigest()


def _text(value, field, limit=240, *, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise CrmOrdersReadError(f"Invalid {field}")
    return value.strip()


def _guid(value, field, *, optional=False):
    if optional and value in (None, "", ZERO_GUID):
        return None
    return normalize_guid(value, field=field)


def _money(value, field):
    if isinstance(value, (bool, float)) or value is None:
        raise CrmOrdersReadError(f"Invalid {field}")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CrmOrdersReadError(f"Invalid {field}") from None
    if (not amount.is_finite() or amount.as_tuple().exponent < -2 or amount.as_tuple().exponent > 20
            or len(amount.as_tuple().digits) > 24 or amount.copy_abs() >= Decimal("1e20")):
        raise CrmOrdersReadError(f"Invalid {field}")
    return amount


def _moment(value, field):
    if not isinstance(value, str) or len(value) > 40:
        raise CrmOrdersReadError(f"Invalid {field}")
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CrmOrdersReadError(f"Invalid {field}") from None
    if moment.year < 1900:
        raise CrmOrdersReadError(f"Invalid {field}")
    return moment.replace(tzinfo=calendar_timezone()) if moment.tzinfo is None else moment


def _bool(raw, field):
    if type(raw.get(field)) is not bool:
        raise CrmOrdersReadError(f"Invalid {field}")
    return raw[field]


def _org(raw, config):
    organization = _guid(raw.get("Организация_Key"), "Организация_Key")
    if organization not in config.organization_guids:
        raise CrmOrdersReadError("CRM order source returned another organization")
    return organization


def _org_filter(config):
    return "(" + " or ".join(f"Организация_Key eq guid'{guid}'" for guid in config.organization_guids) + ")"


def _refs_filter(field, refs):
    # Polymorphic references are strings in this publication, Ref_Key is Edm.Guid.
    literal = "guid'{}'" if field in {"Ref_Key", "Контрагент_Key"} else "'{}'"
    return "(" + " or ".join(f"{field} eq " + literal.format(guid) for guid in refs) + ")"


def _read(config, entity, fields, filters, opener, budget, *, latest_limit=None):
    if entity not in {ORDER, INVOICE, SETTLEMENTS, *RECEIPTS}:
        raise CrmOrdersReadError("Unsupported CRM order entity")
    remaining = config.max_pages - budget[0]
    if remaining <= 0:
        raise CrmOrdersReadError("CRM order read exceeded its page budget")
    url = f"{config.base_url}{quote(entity, safe='')}?$select={quote(','.join(fields))}&$filter={quote(filters)}"
    if latest_limit is not None:
        if entity != ORDER or type(latest_limit) is not int or not 1 <= latest_limit <= DISCOVERY_LIMIT:
            raise CrmOrdersReadError("Invalid bounded order discovery")
        url += f"&$orderby={quote('Date desc,Ref_Key asc')}&$top={latest_limit}"
    result = []
    for rows, _ in read_odata_pages(replace(config, max_pages=remaining), url, opener=opener):
        budget[0] += 1
        budget[1] += len(rows)
        if budget[1] > config.max_rows:
            raise CrmOrdersReadError("CRM order read exceeded its row budget")
        if any(type(row) is not dict for row in rows):
            raise CrmOrdersReadError("Invalid CRM order source row")
        result.extend(rows)
        if latest_limit is not None and len(result) > latest_limit:
            raise CrmOrdersReadError("Order discovery exceeded its requested bound")
    return result


def _discover(config, opener, budget):
    refs = set()
    for raw in _read(config, ORDER, ("Ref_Key", "Date", "Организация_Key"),
            _org_filter(config), opener, budget, latest_limit=DISCOVERY_LIMIT):
        _org(raw, config)
        _moment(raw.get("Date"), "Date")
        ref = _guid(raw.get("Ref_Key"), "Ref_Key")
        if ref in refs:
            raise CrmOrdersReadError("Duplicate order in bounded discovery")
        refs.add(ref)
    return refs


def _discover_number(config, opener, budget, number, customers):
    refs = set()
    # Escape the OData string literal before the whole filter is URL-encoded.
    exact_number = number.replace("'", "''")
    for start in range(0, len(customers), CHUNK):
        chunk = customers[start:start + CHUNK]
        filters = (_org_filter(config) + " and " + _refs_filter("Контрагент_Key", chunk)
            + " and Number eq '" + exact_number + "'")
        for raw in _read(config, ORDER, ("Ref_Key", "Date", "Организация_Key", "Контрагент_Key", "Number"),
                filters, opener, budget):
            _org(raw, config)
            _moment(raw.get("Date"), "Date")
            if (_guid(raw.get("Контрагент_Key"), "Контрагент_Key") not in chunk
                    or _text(raw.get("Number"), "Number", 80) != number):
                raise CrmOrdersReadError("Order number lookup returned another customer or number")
            ref = _guid(raw.get("Ref_Key"), "Ref_Key")
            if ref in refs:
                raise CrmOrdersReadError("Duplicate order in exact number lookup")
            refs.add(ref)
            if len(refs) > MAX_ORDERS:
                raise CrmOrdersReadError("Order number lookup exceeded supported scope")
    return refs


def _header(raw, config, entity):
    result = {"ref": _guid(raw.get("Ref_Key"), "Ref_Key"), "organization": _org(raw, config),
        "customer": _guid(raw.get("Контрагент_Key"), "Контрагент_Key", optional=entity == ORDER) or "",
        "number": _text(raw.get("Number"), "Number", 80, empty=True),
        "version": _text(raw.get("DataVersion"), "DataVersion", 160),
        "date": _moment(raw.get("Date"), "Date"), "posted": _bool(raw, "Posted"),
        "deleted": _bool(raw, "DeletionMark"), "amount": _money(raw.get("СуммаДокумента"), "СуммаДокумента"),
        "type": entity}
    currency_field = "ВалютаДенежныхСредств_Key" if entity in RECEIPTS else "ВалютаДокумента_Key"
    result["currency"] = _guid(raw.get(currency_field), currency_field, optional=True)
    if entity not in RECEIPTS:
        result["agreement"] = _guid(raw.get("Договор_Key"), "Договор_Key", optional=True)
    if entity == ORDER:
        result["cancelled"] = _guid(raw.get("ПричинаОтмены_Key"), "ПричинаОтмены_Key", optional=True) is not None
    if entity == INVOICE:
        result["order"] = _guid(raw.get("Заказ"), "Заказ", optional=True)
        if result["order"] and normalize_document_type(raw.get("Заказ_Type"), field="Заказ_Type") != ORDER:
            raise CrmOrdersReadError("Realization order type differs from customer order")
    if entity != ORDER:
        result["operation"] = _text(raw.get("ВидОперации"), "ВидОперации", 160)
    return result


def _movement(raw, config):
    if _bool(raw, "Active") is not True:
        raise CrmOrdersReadError("Inactive settlement returned for active query")
    line = raw.get("LineNumber")
    if isinstance(line, bool) or not isinstance(line, (str, int)) or not str(line).isdigit() or not 0 < int(line) <= 1000000:
        raise CrmOrdersReadError("Invalid settlement line number")
    order = _guid(raw.get("Заказ"), "Заказ", optional=True)
    if order and normalize_document_type(raw.get("Заказ_Type"), field="Заказ_Type") != ORDER:
        raise CrmOrdersReadError("Settlement order type differs from customer order")
    doc = _guid(raw.get("Документ"), "Документ", optional=True)
    payment = _guid(raw.get("ДокументОплаты"), "ДокументОплаты", optional=True)
    return {"recorder": _guid(raw.get("Recorder"), "Recorder"),
        "recorder_type": normalize_document_type(raw.get("Recorder_Type"), field="Recorder_Type"),
        "line": int(line), "period": _moment(raw.get("Period"), "Period"), "order": order,
        "organization": _org(raw, config), "customer": _guid(raw.get("Контрагент_Key"), "Контрагент_Key"),
        "agreement": _guid(raw.get("Договор_Key"), "Договор_Key", optional=True),
        "direction": _text(raw.get("RecordType"), "RecordType", 30),
        "settlement": _text(raw.get("ТипРасчетов"), "ТипРасчетов", 80),
        "document": doc, "document_type": normalize_document_type(raw.get("Документ_Type"), field="Документ_Type") if doc else None,
        "payment": payment, "payment_type": normalize_document_type(raw.get("ДокументОплаты_Type"), field="ДокументОплаты_Type") if payment else None,
        "amount": _money(raw.get("СуммаВал"), "СуммаВал"),
        "payment_amount": _money(raw.get("СуммаПлатежа"), "СуммаПлатежа"),
        "order_payment_flag": _bool(raw, "ЭтоОплатаЗаказа")}


def _collect(config, selected, now, opener, budget, *, expected_number=None, expected_customers=(), required=()):
    orders, invoices, movements, all_movements, payments = {}, {}, {}, {}, {}
    chunks = [None] if selected is None else [selected[i:i + CHUNK] for i in range(0, len(selected), CHUNK)]
    for chunk in chunks:
        filters = _org_filter(config) + (" and " + _refs_filter("Ref_Key", chunk) if chunk else "")
        for raw in _read(config, ORDER, ORDER_FIELDS, filters, opener, budget):
            row = _header(raw, config, ORDER)
            if row["ref"] in orders or (selected is not None and row["ref"] not in selected):
                raise CrmOrdersReadError("Unexpected or duplicate customer order")
            if expected_number is not None and (row["customer"] not in expected_customers
                    or (row["ref"] not in required and row["number"] != expected_number)):
                raise CrmOrdersReadError("Order lookup scope changed during the read")
            orders[row["ref"]] = row
    if len(orders) > MAX_ORDERS + DISCOVERY_LIMIT:
        raise CrmOrdersReadError("CRM order scope exceeds supported limit")
    parties = sorted({row["customer"] for row in orders.values() if row["customer"]})
    for start in range(0, len(parties), CHUNK):
        chunk = parties[start:start + CHUNK]
        # The published polymorphic Заказ filter has returned incomplete data.
        # Read the complete party scope and select exact orders locally instead.
        filters = _org_filter(config) + " and " + _refs_filter("Контрагент_Key", chunk)
        for raw in _read(config, SETTLEMENTS, MOVEMENT_FIELDS, filters + " and Active eq true", opener, budget):
            _org(raw, config)
            if _guid(raw.get("Контрагент_Key"), "Контрагент_Key") not in chunk:
                raise CrmOrdersReadError("Unexpected settlement counterparty")
            row = _movement(raw, config)
            key = (row["recorder_type"], row["recorder"], row["line"])
            if key in all_movements:
                raise CrmOrdersReadError("Unexpected or duplicate settlement movement")
            all_movements[key] = row
            if row["order"] in orders:
                movements[key] = row
    wanted_invoices = sorted({row["document"] for row in movements.values() if row["document_type"] == INVOICE}
        | {row["recorder"] for row in movements.values() if row["recorder_type"] == INVOICE})
    for start in range(0, len(wanted_invoices), CHUNK):
        chunk = wanted_invoices[start:start + CHUNK]
        filters = _org_filter(config) + " and " + _refs_filter("Ref_Key", chunk)
        for raw in _read(config, INVOICE, INVOICE_FIELDS, filters, opener, budget):
            row = _header(raw, config, INVOICE)
            if row["ref"] in invoices or row["ref"] not in chunk:
                raise CrmOrdersReadError("Unexpected or duplicate realization")
            invoices[row["ref"]] = row
    # Only referenced supported cash receipt headers, never a whole payment ledger.
    for entity in sorted(RECEIPTS):
        wanted = sorted({row["recorder"] for row in movements.values() if row["recorder_type"] == entity})
        for start in range(0, len(wanted), CHUNK):
            chunk = wanted[start:start + CHUNK]
            filters = _org_filter(config) + " and " + _refs_filter("Ref_Key", chunk)
            for raw in _read(config, entity, PAYMENT_FIELDS, filters, opener, budget):
                row = _header(raw, config, entity)
                key = (entity, row["ref"])
                if key in payments or row["ref"] not in chunk:
                    raise CrmOrdersReadError("Unexpected or duplicate cash receipt")
                payments[key] = row
    invoice_totals, payment_totals, ambiguous_payments = {}, {}, set()
    for row in all_movements.values():
        key = (row["recorder_type"], row["recorder"])
        if row["recorder_type"] == INVOICE and row["settlement"] == "Долг" and row["direction"] == "Receipt":
            invoice_totals[row["recorder"]] = invoice_totals.get(row["recorder"], Decimal("0")) + row["amount"]
        if row["recorder_type"] in RECEIPTS:
            payment_totals[key] = payment_totals.get(key, Decimal("0")) + row["payment_amount"]
            if not (row["settlement"] == "Долг" and row["direction"] == "Expense"
                    and row["payment"] == row["recorder"] and row["payment_type"] == row["recorder_type"]
                    and row["amount"] > 0 and row["payment_amount"] == row["amount"]):
                ambiguous_payments.add(key)
    return {"orders": orders, "invoices": invoices, "movements": list(movements.values()),
        "all_movements": list(all_movements.values()), "payments": [payments[key] for key in sorted(payments)],
        "invoice_totals": invoice_totals, "payment_totals": payment_totals, "ambiguous_payments": ambiguous_payments}


def _canonical(collection):
    return _hash({"orders": sorted(collection["orders"].values(), key=lambda row: row["ref"]),
        "invoices": sorted(collection["invoices"].values(), key=lambda row: row["ref"]),
        "movements": sorted(collection["all_movements"], key=lambda row: (row["recorder_type"], row["recorder"], row["line"])),
        "payments": collection["payments"]})


def _public_document(row, kind, *, amount=None, movement=None):
    result = {"kind": kind, "type": row["type"], "ref": row["ref"], "number": row["number"],
        "date": row["date"].isoformat(), "amount": str(row["amount"] if amount is None else amount),
        "version": row["version"], "posted": row["posted"], "deleted": row["deleted"]}
    if movement:
        result["movement_line"] = movement["line"]
        result["invoice_ref"] = movement["document"]
    return result


def _project(order, collection, now, source_id, currency_codes):
    blockers, realization_blockers, payment_blockers = set(), set(), set()
    valid = bool(order["customer"]) and order["posted"] and not order["deleted"] and not order["cancelled"] and order["date"] <= now and order["amount"] > 0
    if not valid:
        blockers.add("order_not_active")
    if not order["currency"]:
        blockers.add("currency_unknown")
    movements = [row for row in collection["movements"] if row["order"] == order["ref"]]
    invoice_refs = {row["document"] for row in movements if row["document_type"] == INVOICE}
    invoice_refs |= {row["recorder"] for row in movements if row["recorder_type"] == INVOICE}
    related = [row for row in collection["invoices"].values() if row["ref"] in invoice_refs]
    payments = {(row["type"], row["ref"]): row for row in collection["payments"]}
    eligible, documents = {}, [_public_document(order, "order")]
    for row in related:
        if any(row[key] != order[key] for key in ("organization", "customer", "agreement", "currency")):
            realization_blockers.add("realization_scope_mismatch")
        elif row["order"] and row["order"] != order["ref"]:
            realization_blockers.add("realization_order_mismatch")
        elif row["operation"] != "ПродажаПокупателю":
            realization_blockers.add("unsupported_realization_operation")
        elif row["date"] > now:
            realization_blockers.add("future_realization")
        elif row["amount"] <= 0:
            realization_blockers.add("realization_correction_requires_review")
        elif row["posted"] and not row["deleted"]:
            eligible[row["ref"]] = row
        else:
            realization_blockers.add("realization_document_not_active")
    if invoice_refs - set(collection["invoices"]):
        realization_blockers.add("realization_document_missing")
    payment_refs = {(row["recorder_type"], row["recorder"]) for row in movements if row["recorder_type"] in RECEIPTS}
    # A correction may lose its order reference while still naming this invoice
    # or payment. Keep it in the evidence graph and never credit an obsolete net.
    for row in collection["all_movements"]:
        related_invoice = ((row["document_type"] == INVOICE and row["document"] in invoice_refs)
            or (row["recorder_type"] == INVOICE and row["recorder"] in invoice_refs))
        related_payment = ((row["recorder_type"], row["recorder"]) in payment_refs
            or (row["payment_type"], row["payment"]) in payment_refs
            or (row["document_type"], row["document"]) in payment_refs)
        if not (related_invoice or related_payment):
            continue
        simple_debt = (row["settlement"] == "Долг" and row["direction"] == "Receipt"
            and row["recorder_type"] == INVOICE and row["document_type"] == INVOICE
            and row["recorder"] == row["document"] and row["payment"] is None
            and row["payment_amount"] == 0 and row["amount"] > 0)
        simple_receipt = (row["settlement"] == "Долг" and row["direction"] == "Expense"
            and row["recorder_type"] in RECEIPTS and row["payment"] == row["recorder"]
            and row["payment_type"] == row["recorder_type"] and row["document_type"] == INVOICE
            and row["amount"] > 0 and row["payment_amount"] == row["amount"])
        scope_fields = ("organization", "customer", "agreement") if related_invoice else ("organization", "customer")
        if (not (simple_debt or simple_receipt) or row["period"] > now
                or any(row[key] != order[key] for key in scope_fields)):
            payment_blockers.add("related_document_requires_review")
            if related_invoice:
                realization_blockers.add("realization_correction_requires_review")
    realized, paid, allocated_per_document = Decimal("0"), Decimal("0"), {}
    realized_per_document = {}
    for row in movements:
        if any(row[key] != order[key] for key in ("organization", "customer", "agreement")):
            payment_blockers.add("settlement_scope_mismatch")
            continue
        if row["period"] > now:
            payment_blockers.add("future_settlement")
            continue
        invoice = eligible.get(row["document"])
        # Invoice debit creates the obligation, even when ЭтоОплатаЗаказа is true.
        debt = (row["settlement"] == "Долг" and row["direction"] == "Receipt" and
            row["recorder_type"] == INVOICE and row["document_type"] == INVOICE and
            row["recorder"] == row["document"] and row["payment"] is None and
            row["payment_amount"] == 0 and row["amount"] > 0 and invoice is not None)
        if debt:
            realized += row["amount"]
            realized_per_document[invoice["ref"]] = realized_per_document.get(invoice["ref"], Decimal("0")) + row["amount"]
            if realized_per_document[invoice["ref"]] > invoice["amount"]:
                realization_blockers.add("realization_allocation_exceeds_document")
            if collection["invoice_totals"].get(invoice["ref"], Decimal("0")) > invoice["amount"]:
                realization_blockers.add("realization_allocation_exceeds_document")
            continue
        receipt = payments.get((row["recorder_type"], row["recorder"]))
        matched = (row["settlement"] == "Долг" and row["direction"] == "Expense" and
            row["recorder_type"] in RECEIPTS and row["payment"] == row["recorder"] and
            row["payment_type"] == row["recorder_type"] and row["document_type"] == INVOICE and
            invoice is not None and row["amount"] > 0 and row["payment_amount"] == row["amount"])
        if not matched:
            payment_blockers.add("unsupported_settlement_operation")
            # Returns/corrections can also invalidate the realized net amount.
            if row["recorder_type"] not in RECEIPTS or row["amount"] < 0:
                realization_blockers.add("realization_correction_requires_review")
            continue
        if receipt is None:
            payment_blockers.add("payment_document_missing")
            continue
        if (not receipt["posted"] or receipt["deleted"] or receipt["date"] > now or receipt["amount"] <= 0):
            payment_blockers.add("payment_document_not_active")
            continue
        if receipt["operation"] != "ОтПокупателя":
            payment_blockers.add("unsupported_payment_operation")
            continue
        if any(receipt[key] != order[key] for key in ("organization", "customer", "currency")):
            payment_blockers.add("payment_document_scope_mismatch")
            continue
        key = (receipt["type"], receipt["ref"])
        allocated_per_document[key] = allocated_per_document.get(key, Decimal("0")) + row["amount"]
        if allocated_per_document[key] > receipt["amount"]:
            payment_blockers.add("payment_allocation_exceeds_document")
        if collection["payment_totals"].get(key, Decimal("0")) > receipt["amount"]:
            payment_blockers.add("payment_allocation_exceeds_document")
        if key in collection["ambiguous_payments"]:
            payment_blockers.add("shared_payment_requires_review")
        paid += row["amount"]
        documents.append(_public_document(receipt, "payment", amount=row["amount"], movement=row))
    for ref, allocated in realized_per_document.items():
        # The public card shows only this order's confirmed allocation, never
        # a foreign document or the whole total of a multi-order realization.
        documents.append(_public_document(eligible[ref], "realization", amount=allocated))
    realization_complete = valid and not blockers and not realization_blockers
    payment_complete = valid and not blockers and not payment_blockers
    all_blockers = tuple(sorted(blockers | realization_blockers | payment_blockers))
    documents = tuple(sorted(documents, key=lambda row: (row["kind"], row["type"], row["ref"], row.get("movement_line", 0))))
    currency_code = currency_codes.get(order["currency"], "")
    return CrmOrderEvidence(order["ref"], order["organization"], order["customer"], order["agreement"],
        order["currency"], order["number"], order["date"], order["amount"],
        realized if realization_complete else None, paid if payment_complete else None,
        realization_complete, payment_complete, valid, order["cancelled"], all_blockers, documents,
        _hash([source_id, currency_code, all_blockers, order, sorted(related, key=lambda row: row["ref"]),
            sorted(movements, key=lambda row: (row["recorder_type"], row["recorder"], row["line"])), documents]), currency_code)


def read_crm_orders(config=None, now=None, opener=None, order_guids=None, required_order_guids=(),
                    order_number=None, customer_guids=()):
    """Read complete current facts for bounded orders, never infer paid from debt.

    Default discovery is limited to the latest 200 orders plus every required
    bound order. Exact-number lookup requires an explicit customer scope and
    searches all history. Missing orders are reported, never returned as zeros.
    Caller owns persistence, CRM permissions, tenant mapping and completion policy.
    """
    config = validate_config(config or config_from_settings())
    if urlsplit(config.base_url).scheme != "https":
        raise CrmOrdersReadError("CRM order source requires HTTPS")
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise CrmOrdersReadError("CRM order source clock must be timezone-aware")
    selected = None
    if type(required_order_guids) not in (list, tuple, set, frozenset) or len(required_order_guids) > MAX_ORDERS:
        raise CrmOrdersReadError("Invalid required customer order selection")
    required = {_guid(value, "required_order_guid") for value in required_order_guids}
    if type(customer_guids) not in (list, tuple, set, frozenset) or len(customer_guids) > MAX_ORDERS:
        raise CrmOrdersReadError("Invalid customer lookup scope")
    customers = sorted({_guid(value, "customer_guid") for value in customer_guids})
    if order_number is not None:
        order_number = _text(order_number, "order number", 80)
        if any(ord(char) < 32 or ord(char) == 127 for char in order_number):
            raise CrmOrdersReadError("Invalid order number")
        if not customers or order_guids is not None:
            raise CrmOrdersReadError("Exact number lookup requires a nonempty customer scope")
    elif customers:
        raise CrmOrdersReadError("Customer lookup scope requires an exact order number")
    if order_guids is not None:
        if type(order_guids) not in (list, tuple, set, frozenset) or len(order_guids) > MAX_ORDERS:
            raise CrmOrdersReadError("Invalid customer order selection")
        selected = sorted({_guid(value, "order_guid") for value in order_guids})
        if required - set(selected):
            raise CrmOrdersReadError("Explicit selection must include required orders")
    source_id = source_identity(config)
    currency_codes = _currency_codes(config)
    opener = opener or build_opener(NoRedirectHandler())
    budget = [0, 0]
    discovery = selected is None
    lookup_scope = {"expected_number": order_number, "expected_customers": customers, "required": required}
    def discover():
        return (_discover_number(config, opener, budget, order_number, customers)
            if order_number is not None else _discover(config, opener, budget))
    with localcontext() as context:
        context.prec = 64
        discovered = discover() if discovery else set()
        if discovery:
            selected = sorted(discovered | required)
        first = _collect(config, selected, now, opener, budget, **lookup_scope)
        second = _collect(config, selected, now, opener, budget, **lookup_scope)
        if _canonical(first) != _canonical(second) or (discovery and discover() != discovered):
            raise CrmOrdersReadError("CRM order source changed during the read; refresh required")
        rows = tuple(_project(first["orders"][key], first, now, source_id, currency_codes) for key in sorted(first["orders"]))
    return CrmOrdersSnapshot(now, datetime.now(timezone.utc), source_id, rows,
        {"schema_version": 1, "complete": True, "pages": budget[0], "rows_read": budget[1],
         "missing_order_guids": sorted(set(selected or []) - set(first["orders"])),
         "read_method": "complete_order_history_twice", "balance_payment_inference": False,
         "discovery_limited": discovery,
         "discovery_mode": "number" if order_number is not None else "recent" if discovery else "explicit",
         "discovery_limit": MAX_ORDERS if order_number is not None else DISCOVERY_LIMIT if discovery else None},
        full_scope=False, requested_order_guids=tuple(selected or []))
