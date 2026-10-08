"""Usage ledger and concurrency-safe budget reservations for call AI.

Prices are USD and versioned. usage_cost_usd is calculated from usage returned
by the API and the stored tariff version; it is not an invoice/confirmed charge.
confirmed_cost_usd stays NULL unless a billing source supplies that value.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from pool_service.call_processing_models import (
    CallProcessingBudget,
    CallProcessingUsage,
)
from pool_service.models import Organization


TARIFF_VERSION = "openai-2026-10-08"
MONEY_QUANT = Decimal("0.000001")
MILLION = Decimal("1000000")

# Official OpenAI API rates checked 2026-10-08.
TOKEN_RATES_USD_PER_MILLION = {
    "gpt-5.6-luna": (Decimal("0.20"), Decimal("1.20")),
    "gpt-4o-transcribe-diarize": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-transcribe": (Decimal("2.50"), Decimal("10.00")),
}
# OpenAI publishes an estimated $0.006/min for gpt-4o-transcribe. The diarize
# model publishes the same token rates but no separate minute estimate. We use
# this only as a clearly-labelled reservation estimate; returned token usage,
# when available, replaces it for the usage-based cost estimate.
TRANSCRIPTION_RESERVE_USD_PER_MINUTE = {
    "gpt-4o-transcribe": Decimal("0.006"),
    "gpt-4o-transcribe-diarize": Decimal("0.006"),
}


class CallBudgetExceeded(RuntimeError):
    pass


def _money(value):
    if value is None:
        return None
    return Decimal(value).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def response_usage_tokens(response):
    usage = getattr(response, "usage", None)
    if usage is None:
        return None, None

    def value(*names):
        for name in names:
            raw = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
            if raw is not None:
                try:
                    return max(0, int(raw))
                except (TypeError, ValueError):
                    return None
        return None

    return value("input_tokens", "prompt_tokens"), value(
        "output_tokens", "completion_tokens"
    )


def token_usage_cost(model, input_tokens, output_tokens):
    rates = TOKEN_RATES_USD_PER_MILLION.get(model)
    if not rates or input_tokens is None or output_tokens is None:
        return None
    input_rate, output_rate = rates
    return _money(
        (Decimal(input_tokens) * input_rate / MILLION)
        + (Decimal(output_tokens) * output_rate / MILLION)
    )


def transcription_reserve_estimate(model, duration_seconds):
    rate = TRANSCRIPTION_RESERVE_USD_PER_MINUTE.get(model)
    if rate is None or duration_seconds is None:
        return None
    seconds = max(0, int(duration_seconds))
    return _money(Decimal(seconds) * rate / Decimal(60))


def analysis_reserve_estimate(model, input_text, max_output_tokens):
    rates = TOKEN_RATES_USD_PER_MILLION.get(model)
    if not rates:
        return None
    input_rate, output_rate = rates
    # UTF-8 byte length is a conservative reservation proxy. Actual returned
    # token usage is stored separately and is not replaced by this estimate.
    input_upper = len((input_text or "").encode("utf-8"))
    output_upper = max(0, int(max_output_tokens))
    return _money(
        (Decimal(input_upper) * input_rate / MILLION)
        + (Decimal(output_upper) * output_rate / MILLION)
    )


def _month_start(now):
    local = timezone.localtime(now) if timezone.is_aware(now) else now
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _effective_row_cost(row):
    if row.status == CallProcessingUsage.STATUS_RESERVED:
        return row.reserved_cost_usd
    return (
        row.confirmed_cost_usd
        if row.confirmed_cost_usd is not None
        else row.usage_cost_usd
        if row.usage_cost_usd is not None
        else row.estimated_cost_usd
    )


@transaction.atomic
def reserve_stage(
    *,
    call,
    attempt_key,
    stage,
    model,
    estimated_cost_usd,
    duration_seconds=None,
):
    """Serialize organization reservations and enforce an owner-selected limit."""
    if stage not in {
        CallProcessingUsage.STAGE_TRANSCRIPTION,
        CallProcessingUsage.STAGE_ANALYSIS,
    }:
        raise ValueError("stage")

    Organization.objects.select_for_update().get(pk=call.organization_id)
    existing = CallProcessingUsage.objects.select_for_update().filter(
        call=call,
        attempt_key=attempt_key,
        stage=stage,
    ).first()
    if existing:
        return existing

    budget, _ = CallProcessingBudget.objects.select_for_update().get_or_create(
        organization_id=call.organization_id
    )
    estimate = _money(estimated_cost_usd)
    if budget.monthly_limit_usd is not None:
        if estimate is None:
            raise CallBudgetExceeded("cost_estimate_unavailable")
        rows = CallProcessingUsage.objects.filter(
            organization_id=call.organization_id,
            created_at__gte=_month_start(timezone.now()),
        ).exclude(status=CallProcessingUsage.STATUS_RELEASED)
        committed = Decimal("0")
        for row in rows.iterator(chunk_size=500):
            cost = _effective_row_cost(row)
            if cost is None:
                raise CallBudgetExceeded("budget_usage_unknown")
            committed += Decimal(cost)
        if committed + estimate > budget.monthly_limit_usd:
            raise CallBudgetExceeded("monthly_budget_exceeded")

    return CallProcessingUsage.objects.create(
        organization_id=call.organization_id,
        call=call,
        employee_id=call.employee_id,
        attempt_key=attempt_key,
        stage=stage,
        model=model,
        status=CallProcessingUsage.STATUS_RESERVED,
        duration_seconds=duration_seconds,
        reserved_cost_usd=estimate,
        estimated_cost_usd=estimate,
        tariff_version=TARIFF_VERSION,
    )


@transaction.atomic
def finish_stage(
    usage_id,
    *,
    succeeded,
    input_tokens=None,
    output_tokens=None,
    error_code="",
):
    usage = CallProcessingUsage.objects.select_for_update().get(pk=usage_id)
    if usage.status != CallProcessingUsage.STATUS_RESERVED:
        return usage
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    usage.usage_cost_usd = token_usage_cost(
        usage.model, input_tokens, output_tokens
    )
    usage.status = (
        CallProcessingUsage.STATUS_SUCCEEDED
        if succeeded
        else CallProcessingUsage.STATUS_FAILED
    )
    usage.error_code = str(error_code or "")[:120]
    usage.finished_at = timezone.now()
    usage.save(update_fields=[
        "input_tokens", "output_tokens", "usage_cost_usd", "status",
        "error_code", "finished_at",
    ])
    return usage


@transaction.atomic
def release_stage(usage_id, *, error_code=""):
    usage = CallProcessingUsage.objects.select_for_update().get(pk=usage_id)
    if usage.status == CallProcessingUsage.STATUS_RESERVED:
        usage.status = CallProcessingUsage.STATUS_RELEASED
        usage.error_code = str(error_code or "")[:120]
        usage.finished_at = timezone.now()
        usage.save(update_fields=["status", "error_code", "finished_at"])
    return usage


def usage_summary(organization, *, now=None):
    now = now or timezone.now()
    rows = list(
        CallProcessingUsage.objects.filter(
            organization=organization,
            created_at__gte=_month_start(now),
        )
        .select_related("employee")
        .order_by("created_at", "id")
    )
    budget = CallProcessingBudget.objects.filter(organization=organization).first()
    totals = {
        "attempts": len(rows),
        "errors": sum(row.status == CallProcessingUsage.STATUS_FAILED for row in rows),
        "reserved_usd": Decimal("0"),
        "estimated_usd": Decimal("0"),
        "usage_usd": Decimal("0"),
        "confirmed_usd": Decimal("0"),
        "confirmed_unknown": 0,
        "audio_seconds": 0,
    }
    by_employee = defaultdict(lambda: {
        "attempts": 0, "audio_seconds": 0,
        "estimated_usd": Decimal("0"), "usage_usd": Decimal("0"),
    })
    by_day = defaultdict(lambda: {
        "attempts": 0, "estimated_usd": Decimal("0"), "usage_usd": Decimal("0"),
    })
    for row in rows:
        totals["audio_seconds"] += int(row.duration_seconds or 0)
        if row.status == CallProcessingUsage.STATUS_RESERVED and row.reserved_cost_usd is not None:
            totals["reserved_usd"] += row.reserved_cost_usd
        if row.estimated_cost_usd is not None:
            totals["estimated_usd"] += row.estimated_cost_usd
        if row.usage_cost_usd is not None:
            totals["usage_usd"] += row.usage_cost_usd
        if row.confirmed_cost_usd is not None:
            totals["confirmed_usd"] += row.confirmed_cost_usd
        else:
            totals["confirmed_unknown"] += 1

        employee_key = row.employee_id or 0
        item = by_employee[employee_key]
        item["employee"] = (
            (row.employee.get_full_name() or row.employee.username)
            if row.employee
            else "Не определён"
        )
        item["attempts"] += 1
        item["audio_seconds"] += int(row.duration_seconds or 0)
        if row.estimated_cost_usd is not None:
            item["estimated_usd"] += row.estimated_cost_usd
        if row.usage_cost_usd is not None:
            item["usage_usd"] += row.usage_cost_usd

        day = timezone.localtime(row.created_at).date()
        daily = by_day[day]
        daily["date"] = day
        daily["attempts"] += 1
        if row.estimated_cost_usd is not None:
            daily["estimated_usd"] += row.estimated_cost_usd
        if row.usage_cost_usd is not None:
            daily["usage_usd"] += row.usage_cost_usd

    for key in ("reserved_usd", "estimated_usd", "usage_usd", "confirmed_usd"):
        totals[key] = _money(totals[key])
    return {
        "budget": budget,
        "tariff_version": TARIFF_VERSION,
        "totals": totals,
        "by_employee": sorted(by_employee.values(), key=lambda item: item["employee"]),
        "by_day": sorted(by_day.values(), key=lambda item: item["date"], reverse=True),
    }
