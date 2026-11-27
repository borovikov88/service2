"""A personal ticket permits a single proposal; only the owner's session can see it."""
import hashlib
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pool_service.models import Employee, Organization, OrganizationAccess
from pool_service.onec_draft_models import OneCDraftRequest
from pool_service.staff_call_models import StaffAccount

from .onec_draft_intent import document_intent, nomenclature_intent


class DraftUnavailable(ValueError):
    pass


class DraftLimit(ValueError):
    pass


def target_organization():
    try:
        value = int(settings.ONEC_ODATA_TARGET_ORGANIZATION_ID)
    except (TypeError, ValueError, AttributeError):
        return None
    return value if value > 0 else None


def is_staff_member(user_id, organization_id):
    if not organization_id:
        return False
    unavailable = Employee.objects.filter(organization_id=organization_id, user_id=user_id).filter(
        Q(is_active=False) | Q(employment_status=Employee.STATUS_DISMISSED)
    ).values("user_id")
    technical = StaffAccount.objects.filter(
        organization_id=organization_id, user_id=user_id, kind__in=("system", "service")
    ).values("user_id")
    return OrganizationAccess.objects.filter(
        organization_id=organization_id, user_id=user_id, user__is_active=True, user__is_superuser=False
    ).exclude(user_id__in=unavailable).exclude(user_id__in=technical).exists()


def _lock_staff(user_id, org_id):
    if not org_id or not Organization.objects.select_for_update().filter(pk=org_id).first():
        raise DraftUnavailable
    get_user_model().objects.select_for_update().filter(pk=user_id).first()
    if not is_staff_member(user_id, org_id):
        raise DraftUnavailable


def _digest(ticket):
    return hashlib.sha256(ticket.encode("ascii")).hexdigest()


@transaction.atomic
def issue(user_id, kind):
    org_id = target_organization()
    _lock_staff(user_id, org_id)
    if getattr(settings, "ONEC_MCP_DIRECT_DRAFTS_ENABLED", False) or kind not in ("document", "nomenclature"):
        raise DraftUnavailable
    now = timezone.now()
    owner_requests = OneCDraftRequest.objects.filter(organization_id=org_id, user_id=user_id)
    limit = getattr(settings, "ONEC_DRAFTS_HOURLY_LIMIT", 10)
    if owner_requests.filter(created_at__gt=now - timedelta(hours=1)).count() >= limit:
        raise DraftLimit
    owner_requests.filter(created_at__lt=now - timedelta(days=2), approved_at__isnull=True,
                          token_hash__isnull=False).delete()
    ticket = secrets.token_urlsafe(32)
    row = owner_requests.create(
        user_id=user_id, organization_id=org_id, kind=kind, token_hash=_digest(ticket),
        expires_at=now + timedelta(minutes=15),
    )
    return row, ticket


@transaction.atomic
def submit(ticket, kind, arguments):
    if getattr(settings, "ONEC_MCP_DIRECT_DRAFTS_ENABLED", False):
        raise DraftUnavailable
    if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
        raise DraftUnavailable
    criteria = {"token_hash": _digest(ticket), "organization_id": target_organization(), "kind": kind}
    actor_id = OneCDraftRequest.objects.filter(**criteria).values_list("user_id", flat=True).first()
    if not actor_id:
        raise DraftUnavailable
    _lock_staff(actor_id, criteria["organization_id"])
    row = OneCDraftRequest.objects.select_for_update().filter(user_id=actor_id, **criteria).first()
    if row is None or row.expires_at <= timezone.now():
        raise DraftUnavailable
    intent = (document_intent if kind == "document" else nomenclature_intent)(arguments)
    if row.intent is None:
        row.intent = intent
        row.submitted_at = timezone.now()
        row.save(update_fields=["intent", "submitted_at"])
    elif row.intent != intent:
        raise DraftUnavailable
    return row.pk


def personal_requests(user_id):
    org_id = target_organization()
    if not is_staff_member(user_id, org_id):
        raise DraftUnavailable
    return OneCDraftRequest.objects.filter(
        user_id=user_id, organization_id=org_id, created_at__gte=timezone.now() - timedelta(days=2)
    ).order_by("-created_at")

@transaction.atomic
def renew_approved(user_id, row_id):
    org_id = target_organization()
    _lock_staff(user_id, org_id)
    row = personal_requests(user_id).select_for_update().filter(pk=row_id, approved_at__isnull=False).first()
    if not row or not row.write_attempts.exists():
        raise DraftUnavailable
    if getattr(settings, "ONEC_MCP_DIRECT_DRAFTS_ENABLED", False):
        if row.token_hash is not None or row.kind != "nomenclature":
            raise DraftUnavailable
        row.expires_at = timezone.now() + timedelta(minutes=15)
        row.save(update_fields=["expires_at"])
        return ""
    ticket = secrets.token_urlsafe(32)
    row.token_hash = _digest(ticket)
    row.expires_at = timezone.now() + timedelta(minutes=15)
    row.save(update_fields=["token_hash", "expires_at"])
    return ticket
