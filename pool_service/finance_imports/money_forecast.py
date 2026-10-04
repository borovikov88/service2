"""Canonical server service for the management "Money" page and Finance MCP."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from pool_service.finance_imports.finance_position import (
    get_cash_position_breakdown,
    get_finance_position,
    get_settlement_position_breakdown,
)
from pool_service.finance_imports.management_finance import (
    get_cashflow_breakdown,
    get_monthly_finance,
)
from pool_service.finance_position_models import (
    OneCFinancePositionSnapshot,
    SettlementPositionRow,
)
from pool_service.money_models import (
    ManagementMoneyPlan,
    OneCMoneyForecastRow,
    OneCMoneyForecastSnapshot,
)

MONEY_CONTRACT_VERSION = "management_money.v1"
ZERO = Decimal("0.00")


def _month_start(value):
    if isinstance(value, date):
        return value.replace(day=1)
    return date.fromisoformat(f"{value}-01")


def _next_month(value):
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def _month_sequence(start, count):
    values = []
    current = start
    for _ in range(count):
        values.append(current)
        current = _next_month(current)
    return values


def _money(value):
    if value is None:
        return None
    return Decimal(value).quantize(Decimal("0.01"))


def _active_snapshot(organization):
    return (
        OneCMoneyForecastSnapshot.objects.filter(
            organization=organization, is_active=True
        )
        .order_by("-source_at", "-id")
        .first()
    )


@transaction.atomic
def persist_money_forecast(organization, user, source, *, sync_run=None):
    """Atomically activate one synced 1C planning snapshot."""
    locked = type(organization).objects.select_for_update().get(pk=organization.pk)
    OneCMoneyForecastSnapshot.objects.filter(
        organization=locked, is_active=True
    ).update(is_active=False)
    snapshot = OneCMoneyForecastSnapshot.objects.create(
        organization=locked,
        sync_run=sync_run,
        source_at=source.source_at,
        fetched_at=source.fetched_at,
        activated_at=timezone.now(),
        is_active=True,
        diagnostics=source.diagnostics,
    )
    OneCMoneyForecastRow.objects.bulk_create([
        OneCMoneyForecastRow(
            snapshot=snapshot,
            direction=row.direction,
            item_kind=row.item_kind,
            source_identity=row.source_identity,
            order_guid=row.order_guid,
            document_guid=row.document_guid,
            document_type=row.document_type,
            agreement_guid=row.agreement_guid,
            counterparty_guid=row.counterparty_guid,
            counterparty_name=row.counterparty_name,
            order_number=row.order_number,
            order_date=row.order_date,
            order_state=row.order_state,
            expected_amount=row.expected_amount,
            matched_paid_amount=row.matched_paid_amount,
            remaining_amount=row.remaining_amount,
            payment_match_status=row.payment_match_status,
            contractual_due_date=row.contractual_due_date,
            expected_date=row.expected_date,
            expected_month=row.expected_month,
            date_precision=row.date_precision,
            basis=row.basis,
            confirmation_status=row.confirmation_status,
            source_updated_at=row.source_updated_at,
            source_payload=row.source_payload,
        )
        for row in source.rows
    ])
    return snapshot


def _active_position_snapshot(organization):
    return (
        OneCFinancePositionSnapshot.objects.filter(
            organization=organization, is_active=True
        )
        .order_by("-snapshot_at", "-id")
        .first()
    )


def _match_order_receivables(organization, source):
    """Reconcile already-due customer schedule rows to current 1C receivables."""
    position = _active_position_snapshot(organization)
    if position is None:
        return source

    receivable_by_order = defaultdict(lambda: ZERO)
    for settlement in position.settlement_rows.filter(
        side=SettlementPositionRow.SIDE_CUSTOMER,
        management_classification=SettlementPositionRow.CLASS_RECEIVABLE,
    ).exclude(order_guid__isnull=True):
        receivable_by_order[str(settlement.order_guid)] += abs(settlement.amount)

    due_by_order = defaultdict(list)
    snapshot_date = position.snapshot_at.date()
    for index, row in enumerate(source.rows):
        if (
            row.direction == "receipt"
            and row.item_kind in {
                OneCMoneyForecastRow.KIND_ORDER_SCHEDULE,
                OneCMoneyForecastRow.KIND_ORDER_DUE,
            }
            and row.order_guid
            and row.expected_date
            and row.expected_date <= snapshot_date
            and row.confirmation_status in {"confirmed", "review"}
            and row.expected_amount is not None
        ):
            due_by_order[str(row.order_guid)].append((index, row))

    replacements = {}
    for order_guid, rows in due_by_order.items():
        total_due = sum((row.expected_amount or ZERO for _, row in rows), ZERO)
        current_receivable = min(receivable_by_order.get(order_guid, ZERO), total_due)

        # 1C normally closes the oldest debt first. Therefore any amount still
        # outstanding belongs to the latest already-due instalments.
        not_allocated = current_receivable
        for index, row in sorted(
            rows,
            key=lambda pair: (
                pair[1].expected_date,
                pair[1].source_identity,
            ),
            reverse=True,
        ):
            amount = row.expected_amount or ZERO
            remaining = min(amount, not_allocated)
            not_allocated = max(not_allocated - remaining, ZERO)
            if remaining > ZERO:
                replacements[index] = replace(
                    row,
                    matched_paid_amount=amount - remaining,
                    remaining_amount=remaining,
                    payment_match_status="order_receivable_matched",
                    confirmation_status="confirmed",
                    basis=f"{row.basis} · остаток по дебиторке 1С",
                )
            else:
                replacements[index] = replace(
                    row,
                    matched_paid_amount=amount,
                    remaining_amount=ZERO,
                    payment_match_status="order_receivable_settled",
                    confirmation_status="excluded",
                    basis=f"{row.basis} · погашено по текущей дебиторке 1С",
                )

    if not replacements:
        return source
    return replace(
        source,
        rows=tuple(replacements.get(index, row) for index, row in enumerate(source.rows)),
    )


def _match_realization_receivables(organization, source):
    """Confirm realization forecast amounts only from the active factual AR snapshot."""
    position = _active_position_snapshot(organization)
    if position is None:
        return source

    receivables = defaultdict(list)
    for row in position.settlement_rows.filter(
        side=SettlementPositionRow.SIDE_CUSTOMER,
        management_classification=SettlementPositionRow.CLASS_RECEIVABLE,
    ).exclude(document_guid__isnull=True):
        receivables[str(row.document_guid)].append(row)

    matched_rows = []
    for row in source.rows:
        if row.item_kind != OneCMoneyForecastRow.KIND_REALIZATION_DUE or not row.document_guid:
            matched_rows.append(row)
            continue
        matches = receivables.get(str(row.document_guid), [])
        if len(matches) == 1:
            remaining = abs(matches[0].amount)
            paid = (
                max((row.expected_amount or ZERO) - remaining, ZERO)
                if row.expected_amount is not None else None
            )
            matched_rows.append(replace(
                row,
                matched_paid_amount=paid,
                remaining_amount=remaining,
                payment_match_status="receivable_document_matched",
                confirmation_status="confirmed",
                basis=f"{row.basis} · остаток по дебиторке 1С",
            ))
        elif len(matches) > 1:
            matched_rows.append(replace(
                row,
                payment_match_status="ambiguous_receivable_document",
                confirmation_status="review",
            ))
        elif row.order_date and position.snapshot_at.date() >= row.order_date:
            # A posted realization absent from a later/equal factual AR snapshot
            # is treated as settled, not as a future receipt.
            matched_rows.append(replace(
                row,
                matched_paid_amount=row.expected_amount,
                remaining_amount=ZERO,
                payment_match_status="no_current_receivable",
                confirmation_status="excluded",
                basis=f"{row.basis} · текущей дебиторки по реализации нет",
            ))
        else:
            matched_rows.append(row)
    return replace(source, rows=tuple(matched_rows))


def sync_money_forecast(organization, user, *, now=None, sync_run=None, config=None, opener=None):
    """Refresh planning inputs after the normal 1C sync; never called by page views."""
    from pool_service.finance_imports.odata_money_forecast import read_money_forecast

    now = now or timezone.now()
    source = read_money_forecast(now, config=config, opener=opener)
    source = _match_order_receivables(organization, source)
    source = _match_realization_receivables(organization, source)
    return persist_money_forecast(
        organization, user, source, sync_run=sync_run
    )


def _plan_item(plan):
    expected_month = plan.expected_month
    if plan.expected_date:
        expected_month = plan.expected_date.replace(day=1)
    return {
        "source": "service2",
        "id": plan.pk,
        "direction": plan.direction,
        "source_type": plan.source_type,
        "order_guid": str(plan.linked_order_guid) if plan.linked_order_guid else None,
        "counterparty_name": plan.counterparty_name,
        "object_name": str(plan.pool) if plan.pool_id else "",
        "order_reference": plan.order_reference,
        "amount": plan.amount,
        "remaining_amount": plan.amount,
        "contractual_due_date": plan.contractual_due_date,
        "expected_date": plan.expected_date,
        "expected_month": expected_month,
        "date_precision": plan.date_precision,
        "basis": plan.basis,
        "confirmation_status": plan.confirmation_status,
        "responsible": plan.updated_by.get_full_name() or plan.updated_by.username,
        "last_updated": plan.updated_at,
        "note": plan.note,
        "service_period_start": plan.service_period_start,
        "service_period_end": plan.service_period_end,
    }



def _add_months(value, months):
    total = value.year * 12 + value.month - 1 + months
    return date(total // 12, total % 12 + 1, 1)


def _service_plan_items(plan):
    """Expand only the explicit contract calendar; never infer a season."""
    if not plan.service_period_start or not plan.service_period_end:
        return [_plan_item(plan)]
    active_months = set(plan.service_active_months or [])
    if not active_months:
        # Missing contract calendar is intentionally non-countable.
        item = _plan_item(plan)
        item["confirmation_status"] = "review"
        item["date_precision"] = "unknown"
        item["expected_date"] = None
        item["expected_month"] = None
        item["basis"] = f"{item['basis']} · не задан календарь обслуживания"
        return [item]

    exceptions = {
        str(value)[:7] for value in (plan.service_exceptions or [])
        if value
    }
    current = plan.service_period_start.replace(day=1)
    end = plan.service_period_end.replace(day=1)
    result = []
    while current <= end:
        if current.month in active_months and current.strftime("%Y-%m") not in exceptions:
            payment_month = _add_months(
                current, plan.service_payment_offset_months or 0
            )
            item = _plan_item(plan)
            item.update({
                "id": f"{plan.pk}:{current.isoformat()}",
                "service_month": current,
                "expected_date": None,
                "expected_month": payment_month,
                "date_precision": "month",
                "basis": f"{plan.basis} · обслуживание {current:%m.%Y}",
            })
            result.append(item)
        current = _next_month(current)
    return result


def _synced_item(row):
    return {
        "source": "onec",
        "id": row.pk,
        "direction": row.direction,
        "source_type": row.item_kind,
        "order_guid": str(row.order_guid) if row.order_guid else None,
        "document_guid": str(row.document_guid) if row.document_guid else None,
        "document_type": row.document_type,
        "counterparty_name": row.counterparty_name,
        "object_name": row.object_name,
        "order_reference": (
            f"№ {row.order_number} от {row.order_date:%d.%m.%Y}"
            if row.order_number and row.order_date
            else row.order_number
        ),
        "amount": row.expected_amount,
        "paid_amount": row.matched_paid_amount,
        "remaining_amount": row.remaining_amount,
        "payment_match_status": row.payment_match_status,
        "contractual_due_date": row.contractual_due_date,
        "expected_date": row.expected_date,
        "expected_month": row.expected_month,
        "date_precision": row.date_precision,
        "basis": row.basis,
        "confirmation_status": row.confirmation_status,
        "responsible": row.responsible_name,
        "last_updated": row.source_updated_at or row.snapshot.fetched_at,
        "order_state": row.order_state,
        "source_payload": row.source_payload,
    }


def _deduplicated_forecast(organization, snapshot):
    synced = []
    if snapshot:
        synced = [_synced_item(row) for row in snapshot.rows.all()]
    manual = []
    for plan in (
        ManagementMoneyPlan.objects.filter(
            organization=organization, is_active=True
        )
        .select_related("pool", "updated_by")
        .order_by("expected_date", "expected_month", "id")
    ):
        if plan.source_type == ManagementMoneyPlan.SOURCE_SERVICE:
            manual.extend(_service_plan_items(plan))
        else:
            manual.append(_plan_item(plan))

    # 1C schedule/due date has priority. A linked Service2 expectation only
    # becomes countable when there is no amount-bearing 1C planning row.
    onec_orders_with_plan = {
        item["order_guid"]
        for item in synced
        if item["order_guid"]
        and item["remaining_amount"] is not None
        and item["confirmation_status"] in {"confirmed", "review"}
    }
    result = list(synced)
    for item in manual:
        if item["order_guid"] and item["order_guid"] in onec_orders_with_plan:
            item = {**item, "confirmation_status": "excluded", "duplicate_reason": "onec_priority"}
        result.append(item)
    return result


def _bucket_forecast(items, *, today, forecast_months):
    months = {month: {"month": month, "receipts": ZERO, "payments": ZERO, "items": []}
              for month in forecast_months}
    current_month = today.replace(day=1)
    overdue = []
    undated = []
    attention = []
    possible = []

    for item in items:
        if item["confirmation_status"] == "possible":
            possible.append(item)
            continue
        if item["confirmation_status"] in {"review", "excluded"}:
            attention.append(item)
        if item["date_precision"] == "unknown" or not item["expected_month"]:
            undated.append(item)
            continue
        is_overdue = bool(item["expected_date"] and item["expected_date"] < today)
        if is_overdue:
            overdue.append(item)
            # Unpaid overdue receipts stay visible in the current cash forecast
            # instead of disappearing into a separate historical bucket.
            if item["direction"] != "receipt":
                continue
            item = {
                **item,
                "is_overdue": True,
                "original_expected_date": item["expected_date"],
                "forecast_carried_to": current_month,
            }
            month = current_month
        else:
            month = item["expected_month"]
        if month not in months:
            continue
        months[month]["items"].append(item)
        if (
            item["confirmation_status"] == "confirmed"
            and item["remaining_amount"] is not None
            and item.get("payment_match_status") not in {
                "ambiguous_prepayment_allocation", "amount_not_verified", "missing_date"
            }
        ):
            if item["direction"] == "receipt":
                months[month]["receipts"] += item["remaining_amount"]
            else:
                months[month]["payments"] += item["remaining_amount"]

    return {
        "months": list(months.values()),
        "overdue": overdue,
        "undated": undated,
        "attention": attention,
        "possible": possible,
    }


def _fact_categories(organization, first_month, last_month):
    data = get_cashflow_breakdown(
        organization,
        first_month,
        last_month,
        group_by="management_category",
    )
    return data


def management_money_data(
    organization,
    *,
    period_start,
    period_end,
    forecast_start=None,
    forecast_month_count=12,
    today=None,
):
    """One shared calculation for the HTML page and Finance MCP."""
    today = today or timezone.localdate()
    first = _month_start(period_start)
    last = _month_start(period_end)
    forecast_first = _month_start(forecast_start or today.replace(day=1))
    forecast_months = _month_sequence(forecast_first, forecast_month_count)

    fact = get_monthly_finance(
        organization, start_month=first, end_month=last, group_by="month"
    )
    fact_categories = _fact_categories(organization, first, last)
    position = get_finance_position(organization)
    cash_rows = get_cash_position_breakdown(organization)
    receivables = get_settlement_position_breakdown(
        organization, side="customer", classification="receivable", limit=200
    )
    payables = get_settlement_position_breakdown(
        organization, side="supplier", classification="payable", limit=200
    )

    snapshot = _active_snapshot(organization)
    forecast_items = _deduplicated_forecast(organization, snapshot)
    forecast = _bucket_forecast(
        forecast_items, today=today, forecast_months=forecast_months
    )
    near_term_end = today + timedelta(weeks=12)
    near_term_exact = sorted(
        [
            item for item in forecast_items
            if item.get("expected_date")
            and today <= item["expected_date"] <= near_term_end
            and item.get("confirmation_status") != "possible"
        ],
        key=lambda item: (
            item["expected_date"],
            0 if item["direction"] == "receipt" else 1,
            item.get("counterparty_name") or "",
        ),
    )

    confirmed_receipts = sum(
        (item["receipts"] for item in forecast["months"]), ZERO
    )
    confirmed_payments = sum(
        (item["payments"] for item in forecast["months"]), ZERO
    )

    payment_coverage_complete = bool(
        snapshot
        and isinstance(snapshot.diagnostics, dict)
        and snapshot.diagnostics.get("payment_coverage_complete") is True
    )
    forecast_balance = None
    if (
        position.get("available")
        and position.get("cash_total") is not None
        and payment_coverage_complete
    ):
        forecast_balance = (
            position["cash_total"] + confirmed_receipts - confirmed_payments
        )

    warnings = list(fact.get("warnings", []))
    if fact_categories.get("warnings"):
        warnings.extend(fact_categories["warnings"])
    if snapshot is None:
        warnings.append({
            "code": "money_forecast_not_synced",
            "message": "Плановые данные 1С ещё не синхронизированы.",
        })
    if not payment_coverage_complete:
        warnings.append({
            "code": "future_payments_incomplete",
            "message": "Полнота будущих платежей не подтверждена; прогноз остатка не рассчитывается.",
        })
    if forecast["attention"]:
        warnings.append({
            "code": "forecast_attention",
            "message": "Есть плановые строки, требующие проверки.",
            "count": len(forecast["attention"]),
        })

    return {
        "contract_version": MONEY_CONTRACT_VERSION,
        "period": {"from": first, "to": last},
        "fact": fact,
        "fact_categories": fact_categories,
        "position": position,
        "cash_rows": cash_rows,
        "receivables": receivables,
        "payables": payables,
        "position_capabilities": {
            "deposits_separate_available": False,
            "restricted_cash_available": False,
            "cash_in_transit_available": position.get("cash_in_transit") is not None,
        },
        "forecast": {
            **forecast,
            "from": forecast_first,
            "months_count": forecast_month_count,
            "confirmed_receipts": confirmed_receipts,
            "confirmed_payments": confirmed_payments,
            "balance": forecast_balance,
            "balance_available": forecast_balance is not None,
            "payment_coverage_complete": payment_coverage_complete,
            "snapshot_at": snapshot.source_at if snapshot else None,
            "last_updated": snapshot.fetched_at if snapshot else None,
            "near_term_exact": near_term_exact,
            "near_term_end": near_term_end,
        },
        "warnings": warnings,
        "source": {
            "fact": "pool_service.finance_imports.management_finance",
            "position": "pool_service.finance_imports.finance_position",
            "forecast": "pool_service.finance_imports.money_forecast",
            "scope": "active_confirmed_facts_plus_active_forecast_snapshot",
        },
    }
