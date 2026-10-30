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


def can_read_serving_runtime(user, organization):
    """Org admin/owner/superuser policy, additionally subject to CRM read access.

    Encapsulated here to avoid importing the large views module (_is_org_admin_or_owner).
    No global permission or membership is created or changed.
    """
    from django.contrib.auth import get_user_model
    from pool_service.models import OrganizationAccess
    from .crm_lead_access import can_view_leads
    if not can_view_leads(user, organization):
        return False
    actor = get_user_model().objects.filter(pk=user.pk, is_active=True).first()
    return bool(actor and (actor.is_superuser or OrganizationAccess.objects.filter(
        user_id=actor.pk, organization=organization, role__in=("owner", "admin")).exists()))
