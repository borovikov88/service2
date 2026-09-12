"""Safe browser adapter for the point-in-time finance-position finalizer.

The existing AUTO_APPLY view owns authentication, organization isolation,
permission checks and cursor validation. This adapter deliberately delegates to
that view first, then runs the shared finance-position finalizer only after a
successful request. It also serializes a small allowlisted finance-position
status without exposing stored OData/transport details.
"""
from django.http import JsonResponse

from pool_service.finance_imports.odata_daily_sync import finalize_finance_position_step
from pool_service.finance_views import (
    _auto_run_payload,
    finance_onec_refresh_apply_detail as _base_detail,
    finance_onec_refresh_apply_status as _base_status,
    finance_onec_refresh_apply_step as _base_step,
)
from pool_service.models import OneCODataSyncRun

SAFE_FINANCE_POSITION_ERROR = "Баланс и расчёты не обновлены; предыдущий снимок сохранён."
VISIBLE_FINANCE_POSITION_STATES = frozenset({"completed", "retryable_error", "failed"})


def _safe_payload(run):
    payload = _auto_run_payload(run)
    progress = dict(payload.get("progress") or {})
    state = (run.progress or {}).get("finance_position_state")
    if state in VISIBLE_FINANCE_POSITION_STATES:
        progress["finance_position_state"] = state
        if state in {"retryable_error", "failed"}:
            progress["finance_position_error"] = SAFE_FINANCE_POSITION_ERROR
            payload["message"] = SAFE_FINANCE_POSITION_ERROR
    payload["progress"] = progress
    return payload


def _authorized_run_after(response, run_id):
    """Refetch only after the base view has authorized this exact run."""
    if response.status_code != 200:
        return None
    return OneCODataSyncRun.objects.filter(
        pk=run_id,
        mode=OneCODataSyncRun.MODE_AUTO_APPLY,
    ).first()


def finance_onec_refresh_apply_step(request, run_id):
    response = _base_step(request, run_id)
    run = _authorized_run_after(response, run_id)
    if run is None:
        return response
    if run.status == OneCODataSyncRun.STATUS_COMPLETED:
        # Idempotent on success; on retryable_error this retries ONLY the
        # point-in-time Balance layer and never reruns the completed monthly sync.
        finalize_finance_position_step(run)
        run.refresh_from_db()
    return JsonResponse(_safe_payload(run))


def finance_onec_refresh_apply_status(request, run_id):
    response = _base_status(request, run_id)
    run = _authorized_run_after(response, run_id)
    return response if run is None else JsonResponse(_safe_payload(run))


def finance_onec_refresh_apply_detail(request, run_id):
    response = _base_detail(request, run_id)
    run = _authorized_run_after(response, run_id)
    return response if run is None else JsonResponse(_safe_payload(run))
