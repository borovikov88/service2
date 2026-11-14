"""Best-effort after-commit wakeup; the SQL inbox remains the durable queue.

Opt-in only after a supervised/periodic host fallback is verified. No consumer,
AI, network request, or waiting for the worker runs in the HTTP process.
"""
import logging
from datetime import datetime, timezone
import subprocess
import sys
import threading
from pathlib import Path

from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

# Literal identifies loaded code, not the Git checkout currently on disk.
SERVING_CONTRACT = "crm-wakeup-serving-v1"
MODULE_LOADED_AT = datetime.now(timezone.utc).isoformat()


def serving_runtime(organization_id):
    enabled = bool(getattr(settings, "CRM_INTAKE_EVENT_WAKEUP_ENABLED", False))
    return {
        "contract": SERVING_CONTRACT,
        "module_loaded_at": MODULE_LOADED_AT,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "event_wakeup_enabled": enabled,
        "configuration_gate": "outside_pilot" if organization_id != 1 else
            "enabled" if enabled else "disabled_by_web_settings",
        "event_gate": "not_observed",
        "scope": "responding_process_only",
    }



def schedule_wakeup(event):
    # Same explicit pilot organization as the existing guarded host wrapper.
    if (not getattr(settings, "CRM_INTAKE_EVENT_WAKEUP_ENABLED", False)
            or event.organization_id != 1 or not event.scope_verified or event.historical
            or event.state not in {"pending", "retry", "awaiting_consumer"}):
        return
    transaction.on_commit(wake, using=event._state.db, robust=True)


def _reap(process):
    # Cleanup only; all bounded business processing is in a separate OS process.
    process.wait()


def wake():
    try:
        from scripts.crm_intake_event_wakeup import launch
        process = launch(Path(settings.BASE_DIR), sys.executable)
        if process is not None:
            threading.Thread(target=_reap, args=(process,), daemon=True,
                name="crm-intake-process-reaper").start()
    except Exception as error:
        # Import failure must still preserve ACK and may not expose its message.
        metadata = ('unexpected_failure', 'NONE', None)
        try:
            from scripts.crm_intake_event_wakeup import failure_metadata
            metadata = failure_metadata(error)
        except Exception:
            pass
        try:
            from scripts.crm_wakeup_diagnostic import record_failure
            record_failure(settings.BASE_DIR, metadata)
        except Exception:
            pass
        logger.warning("crm_intake_wakeup_unavailable code=%s errno=%s real_effective_uid_equal=%s; durable inbox retained", *metadata)
