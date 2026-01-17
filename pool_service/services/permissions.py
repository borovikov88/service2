from __future__ import annotations

from datetime import timedelta
import math

from django.utils import timezone

from pool_service.models import Client, Organization, OrganizationAccess, Pool

TRIAL_DAYS = 14


def trial_ends_at(org: Organization | None):
    if not org or not org.trial_started_at:
        return None
    return org.trial_started_at + timedelta(days=TRIAL_DAYS)


def company_has_access(org: Organization | None, now=None) -> bool:
    if not org:
        return False
    now = now or timezone.now()
    if org.paid_until and org.paid_until >= now:
        return True
    trial_end = trial_ends_at(org)
    if trial_end and trial_end > now:
        return True
    return False


def company_trial_days_left(org: Organization | None, now=None) -> int:
    trial_end = trial_ends_at(org)
    if not trial_end:
        return 0
    now = now or timezone.now()
    delta = trial_end - now
    if delta.total_seconds() <= 0:
        return 0
    return int(math.ceil(delta.total_seconds() / 86400))


def is_personal_free(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if OrganizationAccess.objects.filter(user=user).exists():
        return False
    client = Client.objects.filter(user=user, organization__isnull=True).first()
    if not client:
        return False
    return Pool.objects.filter(client=client).count() == 1


def is_personal_user(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if OrganizationAccess.objects.filter(user=user).exists():
        return False
    return Client.objects.filter(user=user, organization__isnull=True).exists()


def personal_pool(user):
    if not is_personal_user(user):
        return None
    client = Client.objects.filter(user=user, organization__isnull=True).first()
    if not client:
        return None
    return Pool.objects.filter(client=client).first()


def organization_for_user(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    access = OrganizationAccess.objects.filter(user=user).select_related("organization").first()
    if not access:
        return None
    return access.organization


def is_org_access_blocked(user, now=None) -> bool:
    org = organization_for_user(user)
    if not org:
        return False
    return not company_has_access(org, now=now)
