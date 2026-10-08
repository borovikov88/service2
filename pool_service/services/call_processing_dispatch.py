"""Idempotent automatic call-analysis dispatch for activated new calls."""
from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from pool_service.call_processing_models import CallProcessingBudget
from pool_service.communication_models import CallAnalysis, PhoneCall
from pool_service.services.call_ai import (
    request_call_analysis,
    start_requested_call_analysis_worker,
)
from pool_service.services.call_processing_settings import runtime_decision_for_call
from pool_service.services.call_privacy import is_private_call


def dispatch_call_if_ready(call_id, *, start_worker=True):
    """Queue one eligible new call. Never activates rules or backfills history."""
    call = (
        PhoneCall.objects.select_related("organization", "analysis")
        .filter(pk=call_id)
        .first()
    )
    if call is None or call.source_kind != PhoneCall.SOURCE_TELEPHONY:
        return {"queued": False, "reason": "not_telephony"}
    if is_private_call(call):
        return {"queued": False, "reason": "personal"}

    # Auto mode requires an explicit owner-selected monetary ceiling. Manual
    # processing remains available independently.
    budget = CallProcessingBudget.objects.filter(
        organization_id=call.organization_id,
        monthly_limit_usd__isnull=False,
    ).first()
    if budget is None:
        return {"queued": False, "reason": "budget_required"}

    decision = runtime_decision_for_call(call)
    if not decision.selected:
        return {"queued": False, "reason": decision.reason}
    if decision.action == "wait_audio":
        return {"queued": False, "reason": "audio_pending"}

    queued = request_call_analysis(call.pk, allow_reanalysis=False)
    if not queued:
        return {"queued": False, "reason": "already_queued_or_done"}
    worker_started = bool(start_requested_call_analysis_worker()) if start_worker else False
    return {
        "queued": True,
        "reason": decision.reason,
        "worker_started": worker_started,
    }


def recover_auto_dispatch(*, limit=100):
    """Bounded recovery for missed audio-ready dispatches.

    Only calls newer than an activated rule can pass runtime policy, so this
    scan cannot turn activation into an archive backfill.
    """
    limit = max(1, min(int(limit), 500))
    active_from = list(
        __import__(
            "pool_service.call_processing_models",
            fromlist=["CallProcessingRule"],
        ).CallProcessingRule.objects.filter(
            effective_from__isnull=False,
        ).values_list("organization_id", "effective_from")
    )
    if not active_from:
        return {"checked": 0, "queued": 0}

    earliest_by_org = {}
    for organization_id, effective_from in active_from:
        current = earliest_by_org.get(organization_id)
        if current is None or effective_from < current:
            earliest_by_org[organization_id] = effective_from

    candidate_ids = []
    for organization_id, earliest in earliest_by_org.items():
        remaining = limit - len(candidate_ids)
        if remaining <= 0:
            break
        ids = (
            PhoneCall.objects.filter(
                organization_id=organization_id,
                source_kind=PhoneCall.SOURCE_TELEPHONY,
                result=PhoneCall.RESULT_ANSWERED,
                recording_status=PhoneCall.RECORDING_STORED,
                started_at__gte=earliest,
            )
            .exclude(recording_file="")
            .filter(
                analysis__isnull=True
            )
            .order_by("started_at", "pk")
            .values_list("pk", flat=True)[:remaining]
        )
        candidate_ids.extend(ids)

    queued = 0
    for call_id in candidate_ids:
        result = dispatch_call_if_ready(call_id, start_worker=False)
        queued += int(result["queued"])
    if queued:
        start_requested_call_analysis_worker()
    return {"checked": len(candidate_ids), "queued": queued}
