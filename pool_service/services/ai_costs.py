"""Server-side, versioned AI usage and cost presentation."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


PRICING_VERSION = "openai-2026-08-10-long-context-v2"
CODEX_ESTIMATOR_VERSION = "development-codex-complexity-v1"
_MILLION = Decimal("1000000")
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_INPUT_MULTIPLIER = Decimal("2")
LONG_CONTEXT_OUTPUT_MULTIPLIER = Decimal("1.5")

# Prices are USD per million tokens.  Only models listed here can produce a
# calculated cost; metadata must never be able to add a price.
PRICE_TABLE = {
    "gpt-5.6": (Decimal("5.00"), Decimal("0.50"), Decimal("30.00")),
    "gpt-5.6-sol": (Decimal("5.00"), Decimal("0.50"), Decimal("30.00")),
    "gpt-5.6-terra": (Decimal("2.50"), Decimal("0.25"), Decimal("15.00")),
    "gpt-5.6-luna": (Decimal("1.00"), Decimal("0.10"), Decimal("6.00")),
}

# Deterministic planning envelopes, not measured usage. Cached input is kept at
# zero because it cannot be predicted safely before an execution.
CODEX_ESTIMATE_TOKENS = {
    "simple": ((20_000, 4_000), (80_000, 20_000)),
    "standard": ((50_000, 10_000), (200_000, 60_000)),
    "complex": ((100_000, 20_000), (400_000, 120_000)),
}


@dataclass(frozen=True)
class UsageCost:
    model: str | None
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    calculated_cost_usd: str | None
    pricing_version: str | None


def _nonnegative_int(value):
    if isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def calculate_usage_cost(model, input_tokens, cached_input_tokens, output_tokens):
    """Return a fixed historical amount, or None when usage/pricing is unknown."""
    model = str(model or "").strip() or None
    input_tokens = _nonnegative_int(input_tokens)
    cached_input_tokens = _nonnegative_int(cached_input_tokens)
    output_tokens = _nonnegative_int(output_tokens)
    if not model or input_tokens is None or output_tokens is None:
        return None
    cached_input_tokens = cached_input_tokens or 0
    if cached_input_tokens > input_tokens or model not in PRICE_TABLE:
        return None
    long_context = input_tokens > LONG_CONTEXT_THRESHOLD
    if long_context and cached_input_tokens:
        # OpenAI documents the long-context input/output multipliers, but not
        # an unambiguous long-context cached-input rate.  Do not guess costs.
        return None
    normal_input = input_tokens - cached_input_tokens
    normal_price, cached_price, output_price = PRICE_TABLE[model]
    if long_context:
        normal_price *= LONG_CONTEXT_INPUT_MULTIPLIER
        output_price *= LONG_CONTEXT_OUTPUT_MULTIPLIER
    amount = (
        Decimal(normal_input) * normal_price
        + Decimal(cached_input_tokens) * cached_price
        + Decimal(output_tokens) * output_price
    ) / _MILLION
    return amount.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)


def usage_metadata(response):
    """Extract only official Responses API usage fields from an SDK object/dict."""
    usage = getattr(response, "usage", None)
    if usage is None and isinstance(response, dict):
        usage = response.get("usage")
    if usage is None:
        return None

    def value(name):
        result = getattr(usage, name, None)
        if result is None and isinstance(usage, dict):
            result = usage.get(name)
        return result

    details = value("input_tokens_details")
    if details is None:
        details = value("prompt_tokens_details")
    if details is None:
        details = {}
    cached = getattr(details, "cached_tokens", None)
    if cached is None and isinstance(details, dict):
        cached = details.get("cached_tokens")
    model = getattr(response, "model", None)
    if model is None and isinstance(response, dict):
        model = response.get("model")
    return {
        "model": str(model or "").strip() or None,
        "input_tokens": _nonnegative_int(value("input_tokens")),
        "cached_input_tokens": _nonnegative_int(cached),
        "output_tokens": _nonnegative_int(value("output_tokens")),
    }


def usage_record(response):
    usage = usage_metadata(response)
    if usage is None:
        return None
    cost = calculate_usage_cost(**usage)
    usage.update(
        calculated_cost_usd=str(cost) if cost is not None else None,
        pricing_version=PRICING_VERSION if cost is not None else None,
        usage_source="openai_responses",
    )
    return usage


def codex_usage_record(usage):
    """Price trusted cumulative Codex usage without guessing request boundaries."""
    if not isinstance(usage, dict):
        return None
    model = str(usage.get("model") or "").strip() or None
    input_tokens = _nonnegative_int(usage.get("input_tokens"))
    cached_input_tokens = _nonnegative_int(usage.get("cached_input_tokens"))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    if (
        model not in PRICE_TABLE
        or input_tokens is None
        or cached_input_tokens is None
        or output_tokens is None
        or cached_input_tokens > input_tokens
    ):
        return None
    unknown_reason = None
    if input_tokens > LONG_CONTEXT_THRESHOLD:
        cost = None
        unknown_reason = "long_context_per_request_usage_unavailable"
    else:
        cost = calculate_usage_cost(
            model, input_tokens, cached_input_tokens, output_tokens
        )
    return {
        "model": model,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "calculated_cost_usd": str(cost) if cost is not None else None,
        "pricing_version": PRICING_VERSION if cost is not None else None,
        "usage_source": usage.get("usage_source"),
        "workflow_run_id": usage.get("workflow_run_id"),
        "launch_token": usage.get("launch_token"),
        "cost_status": "known" if cost is not None else "unknown",
        "cost_unknown_reason": unknown_reason,
    }


def codex_cost_estimate(complexity, model):
    """Build a versioned forecast through the same trusted pricing engine."""
    ranges = CODEX_ESTIMATE_TOKENS.get(complexity)
    if ranges is None or model not in PRICE_TABLE:
        return None
    minimum = calculate_usage_cost(model, ranges[0][0], 0, ranges[0][1])
    maximum = calculate_usage_cost(model, ranges[1][0], 0, ranges[1][1])
    if minimum is None or maximum is None:
        return None
    return {
        "min_usd": str(minimum),
        "max_usd": str(maximum),
        "model": model,
        "complexity": complexity,
        "estimator_version": CODEX_ESTIMATOR_VERSION,
        "pricing_version": PRICING_VERSION,
        "source": "complexity_baseline",
    }


def estimate_context(metadata, analysis_amount=None):
    estimate = metadata.get("codex_cost_estimate") if isinstance(metadata, dict) else None
    if not isinstance(estimate, dict):
        return None
    try:
        minimum = Decimal(str(estimate["min_usd"]))
        maximum = Decimal(str(estimate["max_usd"]))
    except (KeyError, InvalidOperation, TypeError):
        return None
    if minimum < 0 or maximum < minimum:
        return None
    total_min = minimum + analysis_amount if analysis_amount is not None else None
    total_max = maximum + analysis_amount if analysis_amount is not None else None
    return {
        **estimate,
        "min": minimum,
        "max": maximum,
        "amount_display": f"≈ {display_amount(minimum)}–{display_amount(maximum)}",
        "total_display": (
            f"≈ {display_amount(total_min)}–{display_amount(total_max)}"
            if total_min is not None else None
        ),
    }


def _records(iterations, stage):
    records = []
    for iteration in iterations:
        metadata = iteration.automation_metadata if isinstance(iteration.automation_metadata, dict) else {}
        usage = metadata.get("ai_usage") if isinstance(metadata.get("ai_usage"), dict) else {}
        if usage.get("stage") == stage:
            records.extend(usage.get("calls", []))
    return [dict(record) for record in records if isinstance(record, dict)]


def cost_context(iterations, *, codex_expected=False):
    stages = {}
    known_total = Decimal("0")
    known_count = 0
    for stage, expected in (("primary_analysis", True), ("codex", codex_expected), ("ai_review", False)):
        records = _records(iterations, stage)
        amounts = []
        for record in records:
            try:
                amounts.append(Decimal(str(record["calculated_cost_usd"])))
            except (KeyError, InvalidOperation, TypeError):
                pass
        amount = sum(amounts, Decimal("0")) if amounts else None
        if amount is not None:
            known_total += amount
            known_count += 1
        for record in records:
            if record.get("cost_unknown_reason") == "long_context_per_request_usage_unavailable":
                record["cost_unknown_message"] = (
                    "Токены получены, но точную стоимость нельзя определить по "
                    "доступным aggregate usage данным."
                )
        stages[stage] = {"known": amount is not None, "amount": amount, "records": records, "expected": expected}
    partial = known_count > 0 and any(stage["expected"] and not stage["known"] for stage in stages.values())
    total = known_total if known_count and not partial else None
    return {"analysis": stages["primary_analysis"], "codex": stages["codex"], "review": stages["ai_review"], "total": total, "partial_total": known_total if partial else None}


def display_amount(amount, partial=False):
    if amount is None:
        return "—"
    value = f"${amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,.2f}"
    return value + ("+" if partial else "")
