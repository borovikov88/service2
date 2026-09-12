"""Canonical server-side point-in-time finance-position read model."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib, json, os
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from pool_service.finance_position_models import CashPositionRow, OneCFinancePositionSnapshot, SettlementPositionRow
from pool_service.models import OneCImportBatch, Organization
from .odata_finance_position import FinancePositionReadError, FinancePositionSourceSnapshot, config_from_settings, read_finance_position, validate_finance_position_configuration
from .odata_profit_drafts import is_odata_target_organization

IMPORT_TYPE = "finance_position"
CONTRACT_VERSION = "finance_position.v1"
PARSER_VERSION = "finance-position-1"
ZERO = Decimal("0.00")
DEFAULT_STALE_HOURS = 26

class FinancePositionError(ValidationError): pass

def _require_target_organization(organization):
    if not isinstance(organization, Organization) or organization.pk is None: raise FinancePositionError("Finance position organization is invalid")
    if not is_odata_target_organization(organization): raise PermissionDenied("Finance position is not configured for this organization")

def _require_actor(user):
    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False): raise PermissionDenied("Active authenticated user is required")

def _json_value(value):
    if isinstance(value, Decimal): return format(value, "f")
    if isinstance(value, datetime): return value.isoformat()
    return value

def _content_hash(source):
    payload = {
        "cash": [{k: _json_value(v) for k, v in asdict(row).items()} for row in sorted(source.cash_rows, key=lambda r: r.source_identity)],
        "settlements": [{k: _json_value(v) for k, v in asdict(row).items()} for row in sorted(source.settlement_rows, key=lambda r: r.source_identity)],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def _build_batch(organization, user, source, content_hash, sync_run=None):
    count = len(source.cash_rows) + len(source.settlement_rows)
    # OneCImportBatch historically de-duplicates file imports by file_sha256.
    # A point-in-time Balance read is a new successful version even when its
    # business content is unchanged, so keep the true content hash on the
    # snapshot and use a capture/version hash for the audit batch identity.
    capture_nonce = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    batch_hash = hashlib.sha256(
        f"{content_hash}:{source.snapshot_at.isoformat()}:{source.fetched_at.isoformat()}:{capture_nonce}".encode()
    ).hexdigest()
    return OneCImportBatch.objects.create(
        organization=organization, import_type=IMPORT_TYPE, source_type=OneCImportBatch.SOURCE_ODATA,
        original_filename="finance_position.odata", stored_file="", file_sha256=batch_hash, file_size=0,
        status=OneCImportBatch.STATUS_CONFIRMED, uploaded_by=user, confirmed_by=user, confirmed_at=datetime.now(timezone.utc),
        rows_detected=count, rows_imported=count, warnings_count=source.diagnostics.get("sign_anomaly_count", 0), parser_version=PARSER_VERSION,
        metadata={"schema": CONTRACT_VERSION, "content_hash": content_hash, "snapshot_at": source.snapshot_at.isoformat(), "source_timezone": source.source_timezone, "fetched_at": source.fetched_at.isoformat()}, sync_run=sync_run,
    )

def _persist_snapshot(organization, user, source, *, sync_run=None):
    content_hash = _content_hash(source)
    with transaction.atomic():
        org = Organization.objects.select_for_update().get(pk=organization.pk)
        batch = _build_batch(org, user, source, content_hash, sync_run)
        snapshot = OneCFinancePositionSnapshot.objects.create(batch=batch, organization=org, snapshot_at=source.snapshot_at, source_timezone=source.source_timezone, fetched_at=source.fetched_at, content_hash=content_hash, diagnostics={**source.diagnostics, "schema": CONTRACT_VERSION, "cash_row_count": len(source.cash_rows), "settlement_row_count": len(source.settlement_rows)})
        CashPositionRow.objects.bulk_create([CashPositionRow(snapshot=snapshot, source_kind=r.source_kind, organization_guid=r.organization_guid, account_guid=r.account_guid, reference_type=r.reference_type, display_name=r.display_name, currency_guid=r.currency_guid, agreement_guid=r.agreement_guid, amount=r.amount, amount_currency=r.amount_currency, transfer_document_guid=r.transfer_document_guid, transfer_document_type=r.transfer_document_type, transfer_document_display=r.transfer_document_display, source_identity=r.source_identity) for r in source.cash_rows])
        SettlementPositionRow.objects.bulk_create([SettlementPositionRow(snapshot=snapshot, side=r.side, settlement_type_raw=r.settlement_type_raw, management_classification=r.management_classification, organization_guid=r.organization_guid, counterparty_guid=r.counterparty_guid, counterparty_name=r.counterparty_name, agreement_guid=r.agreement_guid, document_guid=r.document_guid, document_type=r.document_type, order_guid=r.order_guid, order_type=r.order_type, amount=r.amount, amount_currency=r.amount_currency, amount_reg=r.amount_reg, is_sign_anomaly=r.is_sign_anomaly, source_identity=r.source_identity) for r in source.settlement_rows])
        OneCFinancePositionSnapshot.objects.filter(organization=org, is_active=True).exclude(pk=snapshot.pk).update(is_active=False)
        snapshot.is_active = True; snapshot.activated_at = datetime.now(timezone.utc); snapshot.save(update_fields=["is_active", "activated_at"])
    return snapshot

def sync_finance_position(organization, user, *, now=None, opener=None, config=None, sync_run=None):
    _require_target_organization(organization); _require_actor(user)
    config = validate_finance_position_configuration(config or config_from_settings())
    source = read_finance_position(config, now=now, opener=opener)
    return _persist_snapshot(organization, user, source, sync_run=sync_run)

def _active_snapshot(organization):
    return OneCFinancePositionSnapshot.objects.filter(organization=organization, is_active=True, batch__organization=organization, batch__import_type=IMPORT_TYPE, batch__source_type=OneCImportBatch.SOURCE_ODATA, batch__status=OneCImportBatch.STATUS_CONFIRMED).order_by("-snapshot_at", "-id").first()

def _freshness(snapshot, now=None):
    if snapshot is None: return {"status": "missing", "is_stale": True, "age_seconds": None}
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None: raise FinancePositionError("Freshness clock must be timezone-aware")
    age = max(timedelta(0), current - snapshot.fetched_at)
    try: stale_hours = int(os.getenv("ONEC_FINANCE_POSITION_STALE_HOURS", DEFAULT_STALE_HOURS))
    except (TypeError, ValueError): stale_hours = DEFAULT_STALE_HOURS
    stale_hours = stale_hours if 1 <= stale_hours <= 168 else DEFAULT_STALE_HOURS
    return {"status": "stale" if age > timedelta(hours=stale_hours) else "fresh", "is_stale": age > timedelta(hours=stale_hours), "age_seconds": int(age.total_seconds())}

def get_finance_position(organization, *, now=None):
    snapshot = _active_snapshot(organization)
    if snapshot is None:
        return {"contract_version": CONTRACT_VERSION, "cash_regular": ZERO, "cash_kkm": ZERO, "cash_in_transit": ZERO, "cash_total": ZERO, "receivables": ZERO, "customer_advances": ZERO, "payables": ZERO, "supplier_advances": ZERO, "sign_anomaly_count": 0, "sign_anomaly_amount": ZERO, "calculated_position": ZERO, "snapshot_at": None, "source_timezone": None, "last_success_at": None, "freshness": _freshness(None, now)}
    cash = {"regular": ZERO, "kkm": ZERO, "in_transit": ZERO}
    for row in snapshot.cash_rows.values("source_kind", "amount"): cash[row["source_kind"]] += row["amount"] or ZERO
    totals = {c: ZERO for c in (SettlementPositionRow.CLASS_RECEIVABLE, SettlementPositionRow.CLASS_CUSTOMER_ADVANCE, SettlementPositionRow.CLASS_PAYABLE, SettlementPositionRow.CLASS_SUPPLIER_ADVANCE)}
    anomaly_count = 0; anomaly_amount = ZERO
    for row in snapshot.settlement_rows.values("management_classification", "amount", "is_sign_anomaly"):
        amount = row["amount"] or ZERO; classification = row["management_classification"]
        if row["is_sign_anomaly"] or classification == SettlementPositionRow.CLASS_SIGN_ANOMALY: anomaly_count += 1; anomaly_amount += abs(amount); continue
        if classification in totals: totals[classification] += abs(amount)
    cash_total = sum(cash.values(), ZERO); receivables = totals[SettlementPositionRow.CLASS_RECEIVABLE]; customer_advances = totals[SettlementPositionRow.CLASS_CUSTOMER_ADVANCE]; payables = totals[SettlementPositionRow.CLASS_PAYABLE]; supplier_advances = totals[SettlementPositionRow.CLASS_SUPPLIER_ADVANCE]
    return {"contract_version": CONTRACT_VERSION, "cash_regular": cash["regular"], "cash_kkm": cash["kkm"], "cash_in_transit": cash["in_transit"], "cash_total": cash_total, "receivables": receivables, "customer_advances": customer_advances, "payables": payables, "supplier_advances": supplier_advances, "sign_anomaly_count": anomaly_count, "sign_anomaly_amount": anomaly_amount, "calculated_position": cash_total + receivables + supplier_advances - payables - customer_advances, "snapshot_at": snapshot.snapshot_at, "source_timezone": snapshot.source_timezone, "last_success_at": snapshot.fetched_at, "freshness": _freshness(snapshot, now)}

def get_cash_position_breakdown(organization):
    snapshot = _active_snapshot(organization)
    return [] if snapshot is None else list(snapshot.cash_rows.order_by("source_kind", "display_name", "id").values("id", "source_kind", "display_name", "amount", "amount_currency", "transfer_document_display"))

def get_settlement_position_breakdown(organization, *, side=None, classification=None, offset=0, limit=100):
    if side is not None and side not in {"customer", "supplier"}: raise FinancePositionError("Settlement side is invalid")
    allowed = {v for v, _ in SettlementPositionRow.CLASS_CHOICES}
    if classification is not None and classification not in allowed: raise FinancePositionError("Settlement classification is invalid")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0: raise FinancePositionError("Offset is invalid")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200: raise FinancePositionError("Limit is invalid")
    snapshot = _active_snapshot(organization)
    if snapshot is None: return {"count": 0, "items": []}
    rows = snapshot.settlement_rows.all()
    if side: rows = rows.filter(side=side)
    if classification: rows = rows.filter(management_classification=classification)
    rows = rows.order_by("-amount", "counterparty_name", "id"); count = rows.count()
    return {"count": count, "items": list(rows[offset:offset + limit].values("id", "side", "settlement_type_raw", "management_classification", "counterparty_name", "amount", "amount_currency", "is_sign_anomaly", "document_type", "order_type"))}
