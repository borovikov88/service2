"""Scheduled/resumed finance jobs. No network or writes from configuration checks."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import time as clock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from pool_service.models import OneCODataSyncRun, Organization
from .odata_payroll_drafts import auto_coverage_config
from .odata_profit import validate_config
from .odata_profit_drafts import config_from_settings
from .odata_unified_sync import (
    SUPPORTED_REPORT_TYPES, SyncConflictError, _add_months, _has_report_permission,
    start_unified_sync, step_unified_sync,
)


class DailyConfigError(ValidationError):
    pass


@dataclass(frozen=True)
class DailyConfig:
    organization_id: int
    user_id: int | None
    enabled: bool
    zone: ZoneInfo
    at: time
    months: int


def daily_config():
    try:
        org = int(settings.ONEC_ODATA_TARGET_ORGANIZATION_ID)
        enabled = settings.ONEC_FINANCE_DAILY_ENABLED
        if type(enabled) is not bool:
            raise ValueError
        raw_user = settings.ONEC_FINANCE_SYNC_USER_ID
        user = int(raw_user) if raw_user else None
        raw_time = settings.ONEC_FINANCE_DAILY_TIME
        if not isinstance(raw_time, str) or len(raw_time) != 5:
            raise ValueError
        at = datetime.strptime(raw_time, "%H:%M").time()
        zone = ZoneInfo(settings.ONEC_FINANCE_TIME_ZONE)
        months = int(settings.ONEC_FINANCE_LOOKBACK_MONTHS)
        if org < 1 or (user is not None and user < 1) or not 1 <= months <= 24:
            raise ValueError
        if enabled and user is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError, ZoneInfoNotFoundError):
        raise DailyConfigError("Проверьте организацию, пользователя, время, часовой пояс и период ONEC_FINANCE_*.") from None
    return DailyConfig(org, user, enabled, zone, at, months)


def _actor(user_id, organization, report_types):
    user = get_user_model().objects.filter(pk=user_id, is_active=True).first()
    if user is None or not report_types or not set(report_types).issubset(SUPPORTED_REPORT_TYPES):
        raise PermissionDenied
    if not all(_has_report_permission(user, organization, item) for item in report_types):
        raise PermissionDenied
    return user


def check_configuration():
    """DB SELECTs only, no 1C, snapshots, schedule markers or imports."""
    config = daily_config()
    organization = Organization.objects.get(pk=config.organization_id)
    _actor(config.user_id, organization, SUPPORTED_REPORT_TYPES)
    validate_config(config_from_settings())
    auto_coverage_config()
    return config


def _select_run(config, now):
    with transaction.atomic():
        organization = Organization.objects.select_for_update().get(pk=config.organization_id)
        active = OneCODataSyncRun.objects.filter(
            organization=organization,
            status__in=[OneCODataSyncRun.STATUS_PENDING, OneCODataSyncRun.STATUS_RUNNING],
        ).first()
        if active:
            # A preview belongs to its operator; never turn it into automatic apply.
            return (active if active.mode == OneCODataSyncRun.MODE_AUTO_APPLY else None), "busy"
        local = now.astimezone(config.zone)
        if not config.enabled or local.time() < config.at:
            return None, "not_due"
        day = local.date().isoformat()
        if OneCODataSyncRun.objects.filter(
            organization=organization, mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            sync_scope___schedule_day=day,
        ).exists():
            return None, "already_attempted"
        user = _actor(config.user_id, organization, SUPPORTED_REPORT_TYPES)
        auto_coverage_config()
        end = local.date().replace(day=1)
        run, _ = start_unified_sync(
            organization, user, SUPPORTED_REPORT_TYPES,
            mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            period_start=_add_months(end, -(config.months - 1)), period_end=end,
            schedule_day=day,
        )
        return run, "scheduled"


def _worker_note(run_id, **values):
    with transaction.atomic():
        run = OneCODataSyncRun.objects.select_for_update().get(pk=run_id)
        progress = dict(run.progress or {})
        progress.update(values)
        run.progress = progress
        run.save(update_fields=["progress"])


def _permission_failure(run_id):
    with transaction.atomic():
        run = OneCODataSyncRun.objects.select_for_update().get(pk=run_id)
        if run.status not in OneCODataSyncRun.TERMINAL_STATUSES:
            run.status = OneCODataSyncRun.STATUS_FAILED
            run.finished_at = timezone.now()
            run.error_message = "Право инициатора на обновление данных было отозвано. Данные не применены."
            run.progress = {**run.progress, "step_state": "permission_revoked", "outcome": "failed"}
            run.save(update_fields=["status", "finished_at", "error_message", "progress"])
        return run


def worker_tick(*, max_steps=4, max_seconds=240, now=None):
    if not 1 <= max_steps <= 100 or not 1 <= max_seconds <= 240:
        raise DailyConfigError("Недопустимый лимит worker.")
    config = daily_config()
    now = now or timezone.now()
    try:
        run, state = _select_run(config, now)
    except SyncConflictError:
        return {"state": "busy", "steps": 0}
    if run is None:
        return {"state": state, "steps": 0}
    progress = run.progress or {}
    if progress.get("worker_retry_cursor") == run.cursor.get("version", 0):
        try:
            retry_at = datetime.fromisoformat(progress["worker_retry_after"])
            if timezone.is_aware(retry_at) and retry_at > now:
                return {"state": "retry_later", "run": str(run.pk), "steps": 0}
        except (KeyError, TypeError, ValueError):
            pass
    _worker_note(run.pk, worker_seen_at=now.isoformat())
    started = clock.monotonic()
    steps = 0
    while steps < max_steps and clock.monotonic() - started < max_seconds:
        run.refresh_from_db()
        if run.status in OneCODataSyncRun.TERMINAL_STATUSES:
            break
        try:
            # Resume with the original initiator, not a more privileged cron identity.
            user = _actor(run.requested_by_id, run.organization, run.requested_report_types)
            version = run.cursor.get("version", 0)
            run = step_unified_sync(
                run.pk, user, run.requested_report_types, version,
                mode=OneCODataSyncRun.MODE_AUTO_APPLY,
            )
        except PermissionDenied:
            run = _permission_failure(run.pk)
            break
        steps += 1
        if (run.progress or {}).get("step_state") == "retryable_error":
            _worker_note(run.pk,
                worker_retry_cursor=run.cursor.get("version", 0),
                worker_retry_after=(timezone.now() + timedelta(minutes=15)).isoformat())
            break
        if run.cursor.get("version", 0) == version:
            break  # Busy lease, terminal result, or another worker owns this cursor.
    return {"state": run.status, "run": str(run.pk), "steps": steps}
