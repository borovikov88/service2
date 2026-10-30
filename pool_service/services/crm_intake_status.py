"""Honest run status without enumerating private communication sources."""
from django.utils import timezone


def intake_status(organization, activation, *, now=None):
    now = now or timezone.now()
    attempted = activation.last_run_at if activation else None
    failed = bool(activation and activation.last_error)
    # No run history exists: after an error we cannot recover an earlier success.
    success = attempted if attempted and not failed else None
    return {
        "enabled": organization.crm_intake_process,
        "last_run_at": attempted.isoformat() if attempted else None,  # compatibility
        "last_attempt_at": attempted.isoformat() if attempted else None,
        "last_attempt_result": "error" if attempted and failed else "complete" if attempted else "unknown",
        "last_successful_pass_at": success.isoformat() if success else None,
        "attempt_age_seconds": max(0, int((now-attempted).total_seconds())) if attempted else None,
        "needs_attention": failed,
        # Unlinked inbox rows have no reusable team-readable lead-source scope.
        # Global org counts could reveal private calls or inaccessible channels.
        "backlog": {"status": "unavailable", "count": None},
    }
