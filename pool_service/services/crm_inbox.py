"""Transactional inbox for saved incoming sources, with a SQL-only adapter seam.

All capture callers hold connection -> organization -> source locks. Claims use
inbox-only transactions; delivery reacquires source locks before the inbox lock.
The lead adapter is selected explicitly by the bounded worker; no network work.
"""
import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.db import transaction
from django.db.models import Count, Max, Min, Q
from django.utils import timezone

from pool_service.communication_models import ChannelConnection, Conversation, ConversationMessage, PhoneCall, TelephonyConnection
from pool_service.crm_inbox_models import CrmInboxBoundary, CrmInboxCursor, CrmInboxEvent
from pool_service.models import Organization

MAX_ATTEMPTS = 5
LEASE_SECONDS = 60


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def provider_timestamp(value):
    """Only provider epoch seconds; missing/invalid is explicitly unknown."""
    if type(value) not in (int, float):
        return None
    try:
        stamp = datetime.fromtimestamp(value, tz=datetime_timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp if 2000 <= stamp.year <= 2100 else None


def source_scope(source_type, source, connection, telephony=None):
    if source_type == "message":
        conv = source.conversation
        return dict(organization=conv.organization_id, provider=connection.channel.kind,
            connection=connection.pk, channel=connection.pk, account=str(connection.external_id),
            namespace=conv.external_id, external=str(source.external_id or ""))
    return dict(organization=source.organization_id, provider="megafon", connection=telephony.pk,
        channel=connection.pk, account=str(telephony.external_id), namespace="", external=str(source.external_id))


def initialize_source(source, scope, *, occurred_at=None, origin="live", historical=False):
    """Initial classification is sticky; provider facts may evolve on a call."""
    if source.intake_received_at is not None:
        if source.intake_scope != scope:
            raise ValueError("inbox_source_binding_changed")
        return
    boundary, _ = CrmInboxBoundary.objects.get_or_create(key="sources")
    source.intake_scope = scope
    source.intake_cutover_at = boundary.captured_at
    source.intake_received_at = timezone.now()
    source.intake_occurred_at = occurred_at
    source.intake_origin = origin
    source.intake_historical = bool(historical or origin == "history"
        or origin == "recovery" and occurred_at is None
        or occurred_at is not None and occurred_at < boundary.captured_at)
    if isinstance(source, PhoneCall):
        source.intake_revision_historical = source.intake_historical


def fingerprint(source_type, source):
    if source_type == "message":
        return _digest(["message", 1])
    return _digest([source.direction, source.result, source.duration_seconds,
        source.intake_occurred_at.isoformat() if source.intake_occurred_at else None,
        source.provider_user, source.provider_extension, source.intake_revision_historical])


def capture_source(source_type, source):
    """Required capture, in the source transaction; no client/AI/notifications."""
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("inbox_capture_requires_transaction")
    if source_type == "message":
        if source.direction != "in" or source.conversation.connection.channel.kind not in {"avito", "website"}:
            return None
        org_id, connection_id = source.conversation.organization_id, source.conversation.connection_id
        provider = source.conversation.connection.channel.kind
        namespace = source.conversation.external_id
    else:
        if source.source_kind != "telephony" or source.direction != "in" or not source.connection_id:
            return None
        org_id, connection_id, provider, namespace = source.organization_id, source.connection_id, "megafon", ""
    scope = source.intake_scope
    verified = bool(scope and source.intake_received_at)
    if verified:
        if scope.get("organization") != org_id or scope.get("connection") != connection_id:
            raise ValueError("inbox_source_binding_changed")
        if source_type == "message":
            actual = source_scope("message", source, source.conversation.connection)
            if actual != scope:
                raise ValueError("inbox_source_binding_changed")
    else:
        # A legacy locator is retained without claiming today's account as evidence.
        scope = dict(organization=org_id, provider=provider, connection=connection_id,
            channel=connection_id, account="", namespace=namespace, external=str(source.external_id or ""))
    if source_type == "call" and verified:
        historical = source.intake_historical or source.intake_revision_historical or bool(
            source.intake_occurred_at and source.intake_cutover_at and source.intake_occurred_at < source.intake_cutover_at)
        if historical and not source.intake_revision_historical:
            source.intake_revision_historical = True
            source.save(update_fields=["intake_revision_historical"])
    key = _digest([org_id, source_type, scope["provider"], scope["connection"], scope["account"],
        scope["namespace"], scope["external"] or ["local", source.pk], None if verified else source.pk])
    latest = CrmInboxEvent.objects.select_for_update().filter(source_type=source_type, source_id=source.pk).order_by("-revision").first()
    digest = fingerprint(source_type, source)
    if latest:
        if latest.source_key != key:
            raise ValueError("inbox_source_binding_changed")
        if latest.fingerprint == digest:
            return latest
    return CrmInboxEvent.objects.create(organization_id=org_id, source_type=source_type, source_id=source.pk,
        source_key=key, provider_kind=scope["provider"], connection_id_snapshot=scope["connection"],
        channel_id_snapshot=scope["channel"], account_id_snapshot=scope["account"], namespace=scope["namespace"],
        external_id=scope["external"], scope_verified=verified, revision=(latest.revision + 1 if latest else 1),
        fingerprint=digest, occurred_at=source.intake_occurred_at if verified else None,
        received_at=source.intake_received_at or source.created_at, origin=source.intake_origin if verified else "legacy",
        historical=(source.intake_revision_historical if source_type == "call" else source.intake_historical) if verified else True,
        state="pending" if verified else "blocked", error_code="" if verified else "legacy_scope_unknown")


def _locked_source(source_type, source_id, channel_id, organization_id):
    connection = ChannelConnection.objects.select_for_update().select_related("channel").filter(pk=channel_id).first()
    org = Organization.objects.select_for_update().filter(pk=organization_id).first()
    if not connection or not org or connection.channel.organization_id != organization_id or not connection.is_active or not connection.channel.is_active:
        return org, None, "source_scope_changed"
    if source_type == "message":
        peek = ConversationMessage.objects.filter(pk=source_id).values("conversation_id").first()
        if not peek:
            return org, None, "source_missing"
        conv = Conversation.objects.select_for_update().select_related("connection__channel", "client").filter(pk=peek["conversation_id"]).first()
        source = ConversationMessage.objects.select_for_update().filter(pk=source_id).first()
        if not source or conv.organization_id != organization_id or conv.connection_id != connection.pk or source.direction != "in" or connection.channel.kind not in {"website", "avito"}:
            return org, None, "source_scope_changed"
        if conv.client_id and conv.client.organization_id != organization_id:
            return org, None, "source_scope_changed"
        source.conversation = conv
        scope = source_scope("message", source, connection)
    else:
        peek = PhoneCall.objects.filter(pk=source_id).values("connection_id").first()
        telephony = TelephonyConnection.objects.select_for_update().filter(pk=peek["connection_id"]).first() if peek else None
        source = PhoneCall.objects.select_for_update().select_related("client", "employee_profile").filter(pk=source_id).first()
        if not source:
            return org, None, "source_missing"
        if (not telephony or not telephony.is_active or telephony.organization_id != organization_id
                or telephony.external_id != connection.external_id or connection.channel.kind != "megafon"
                or source.organization_id != organization_id or source.direction != "in" or source.source_kind != "telephony"
                or source.client_id and source.client.organization_id != organization_id
                or source.employee_profile_id and source.employee_profile.organization_id != organization_id):
            return org, None, "source_scope_changed"
        scope = source_scope("call", source, connection, telephony)
    if source.intake_scope != scope or source.intake_received_at is None:
        return org, None, "source_scope_changed"
    return org, source, ""


def claim_one(*, organization_id, source_type, connection_id, now=None, lease_seconds=LEASE_SECONDS):
    now = now or timezone.now()
    if not 1 <= lease_seconds <= 300:
        raise ValueError("invalid_lease")
    if not Organization.objects.filter(pk=organization_id, crm_intake_process=True).exists():
        return None
    with transaction.atomic():
        # No source/organization lock is acquired while holding an inbox lock.
        due = Q(state__in=["pending", "retry", "awaiting_consumer"], next_attempt_at__lte=now) | Q(state="claimed", lease_until__lte=now)
        event = CrmInboxEvent.objects.select_for_update().filter(due, organization_id=organization_id,
            source_type=source_type,
            connection_id_snapshot=connection_id).order_by("pk").first()
        if event is None:
            return None
        event.state, event.lease_token = "claimed", uuid.uuid4()
        event.lease_until = now + timedelta(seconds=lease_seconds)
        event.save()
        return event.pk, event.lease_token


def _release(event, state, code="", *, due=None):
    event.state, event.error_code = state, code
    event.lease_token = event.lease_until = None
    event.next_attempt_at = due
    event.save()


def deliver_claim(event_id, token, *, consumer=None, now=None):
    """An adapter may perform idempotent SQL work in this transaction only.

    No import-by-string or CLI adapter loading is provided. Network effects
    cannot be atomic with this ack.
    """
    peek = CrmInboxEvent.objects.filter(pk=event_id).first()
    if peek is None:
        return "stale_claim"
    try:
        with transaction.atomic():
            org, source, error = _locked_source(peek.source_type, peek.source_id, peek.channel_id_snapshot, peek.organization_id)
            now = now or timezone.now()
            event = CrmInboxEvent.objects.select_for_update().get(pk=event_id)
            if event.state != "claimed" or event.lease_token != token or event.lease_until <= now:
                return "stale_claim"
            if error:
                _release(event, "blocked", error)
                return "blocked"
            snapshot = tuple(getattr(event, name) for name in ("organization_id", "provider_kind", "connection_id_snapshot",
                "channel_id_snapshot", "account_id_snapshot", "namespace", "external_id"))
            live = tuple(source.intake_scope.get(name) for name in ("organization", "provider", "connection", "channel", "account", "namespace", "external"))
            if not event.scope_verified or snapshot != live:
                _release(event, "blocked", "source_scope_changed")
                return "blocked"
            if not org.crm_intake_process:
                _release(event, "pending", "process_disabled", due=now)
                return "disabled"
            latest_revision = CrmInboxEvent.objects.filter(source_type=event.source_type, source_id=event.source_id).aggregate(value=Max("revision"))["value"]
            if event.revision != latest_revision or fingerprint(event.source_type, source) != event.fingerprint:
                # Preserve the new facts atomically before retiring the old revision.
                capture_source(event.source_type, source)
                _release(event, "superseded")
                return "superseded"
            event.prepared_at = now
            if consumer is None:
                _release(event, "awaiting_consumer", "consumer_unavailable", due=now + timedelta(seconds=60))
                return "awaiting_consumer"
            # Locking Organization serializes supported flag changes. Re-read after
            # adapter too: even an in-transaction switch OFF rolls back its effect.
            outcome = consumer(event, source, f"{event.source_key}:{event.revision}")
            if not Organization.objects.get(pk=org.pk).crm_intake_process:
                raise ProcessDisabled()
            now = max(now, timezone.now())
            if event.lease_until <= now:
                raise ClaimExpired()
            event.attempts += 1
            event.processed_at = now
            from .crm_lead_intake import INTAKE_OUTCOMES
            _release(event, "done", outcome if isinstance(outcome, str) and outcome in INTAKE_OUTCOMES else "")
            return "done"
    except ClaimExpired:
        code = "lease_expired"
    except ProcessDisabled:
        code = "process_disabled"
    except Exception:
        # Exception text can contain source content or provider credentials.
        code = "consumer_failed"
    now = now or timezone.now()
    with transaction.atomic():
        event = CrmInboxEvent.objects.select_for_update().get(pk=event_id)
        if event.state != "claimed" or event.lease_token != token:
            return "stale_claim"
        if code in {"process_disabled", "lease_expired"}:
            _release(event, "pending", code, due=now)
            return "disabled" if code == "process_disabled" else "lease_expired"
        event.attempts += 1
        exhausted = event.attempts >= MAX_ATTEMPTS
        _release(event, "failed" if exhausted else "retry", code,
            due=None if exhausted else now + timedelta(seconds=min(3600, 30 * 2 ** (event.attempts - 1))))
        return event.state


class ClaimExpired(Exception):
    pass


class ProcessDisabled(Exception):
    pass


def run_worker(*, organization_id, source_type, connection_id, limit=100, seconds=10, consumer=None):
    _bounds(limit, seconds)
    deadline = time.monotonic() + seconds
    results = {}
    for _ in range(limit):
        if time.monotonic() >= deadline:
            break
        claim = claim_one(organization_id=organization_id, source_type=source_type, connection_id=connection_id)
        if claim is None:
            break
        state = deliver_claim(*claim, consumer=consumer)
        results[state] = results.get(state, 0) + 1
    return results


def retry_failed(*, organization_id, source_type, connection_id, limit=100):
    _bounds(limit, 1)
    with transaction.atomic():
        events = CrmInboxEvent.objects.select_for_update().filter(organization_id=organization_id,
            source_type=source_type, connection_id_snapshot=connection_id, state="failed").order_by("pk")[:limit]
        count = 0
        for event in events:
            event.attempts = 0
            _release(event, "pending", due=timezone.now())
            count += 1
        return count


def _bounds(limit, seconds):
    if type(limit) is not int or not 1 <= limit <= 1000 or not 0 < seconds <= 60:
        raise ValueError("invalid_bounds")


def _connection(organization_id, source_type, connection_id):
    if source_type == "message":
        row = ChannelConnection.objects.select_related("channel").filter(pk=connection_id,
            channel__organization_id=organization_id, channel__kind__in=["website", "avito"], is_active=True,
            channel__is_active=True).first()
    elif source_type == "call":
        telephony = TelephonyConnection.objects.filter(pk=connection_id, organization_id=organization_id, is_active=True).first()
        matches = list(ChannelConnection.objects.select_related("channel").filter(channel__organization_id=organization_id,
            channel__kind="megafon", external_id=telephony.external_id if telephony else None,
            is_active=True, channel__is_active=True)[:2])
        row = matches[0] if len(matches) == 1 else None
    else:
        row = None
    if row is None:
        raise ValueError("inbox_connection_unavailable")
    return row


def reconcile(*, organization_id, source_type, connection_id, limit=100, seconds=10,
              dry_run=False, include_legacy=False, after_id=0, through_id=None):
    """O(N) cyclic local-source reconciliation; never calls a provider.

    Default scans metadata-equipped sources only. Legacy selection must be an
    explicit finite range and produces blocked, historical, unverified evidence.
    A cursor never passes a failed source; row capture and progress commit together.
    """
    _bounds(limit, seconds)
    if after_id < 0 or through_id is not None and through_id <= after_id or include_legacy and through_id is None:
        raise ValueError("explicit_legacy_range_required")
    connection = _connection(organization_id, source_type, connection_id)
    account = str(connection.external_id)
    if source_type == "message":
        query = ConversationMessage.objects.filter(conversation__organization_id=organization_id,
            conversation__connection_id=connection_id, direction="in")
    else:
        query = PhoneCall.objects.filter(organization_id=organization_id, connection_id=connection_id,
            source_kind="telephony", direction="in")
    if not include_legacy:
        query = query.filter(intake_received_at__isnull=False, intake_scope__account=account,
            intake_scope__organization=organization_id, intake_scope__connection=connection_id)
    query = query.filter(pk__gt=after_id)
    if through_id is not None:
        query = query.filter(pk__lte=through_id)
    scope = dict(organization_id=organization_id, source_type=source_type,
        connection_id_snapshot=connection_id, account_id_snapshot=account, include_legacy=include_legacy, after_id=after_id,
        requested_through_id=through_id or 0)
    if dry_run:
        existing = CrmInboxCursor.objects.filter(**scope).first()
        preview = query
        if existing and existing.through_id:
            preview = preview.filter(pk__gt=existing.last_id, pk__lte=existing.through_id)
        return {"checked": len(list(preview.order_by("pk").values_list("pk", flat=True)[:limit])), "dry_run": True}
    deadline = time.monotonic() + seconds
    checked = 0
    for _ in range(limit):
        if time.monotonic() >= deadline:
            break
        try:
            with transaction.atomic():
                locked = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=connection.pk)
                Organization.objects.select_for_update().get(pk=organization_id)
                if (not locked.is_active or not locked.channel.is_active or locked.channel.organization_id != organization_id
                        or str(locked.external_id) != account):
                    raise ValueError("source_scope_changed")
                cursor, _ = CrmInboxCursor.objects.select_for_update().get_or_create(**scope)
                if cursor.through_id == 0:
                    cursor.through_id = through_id or query.aggregate(value=Max("pk"))["value"] or after_id
                    cursor.last_id = after_id
                source_id = query.filter(pk__gt=cursor.last_id, pk__lte=cursor.through_id).order_by("pk").values_list("pk", flat=True).first()
                if source_id is None:
                    cursor.last_id, cursor.through_id = after_id, 0
                    cursor.cycles += 1
                    cursor.error_code = ""
                    cursor.save()
                    return {"checked": checked, "cycle_complete": True}
                # Legacy sources have no historical account claim to validate.
                if source_type == "message":
                    peek = ConversationMessage.objects.get(pk=source_id)
                    conv = Conversation.objects.select_for_update().select_related("connection__channel").get(pk=peek.conversation_id)
                    source = ConversationMessage.objects.select_for_update().get(pk=source_id)
                    source.conversation = conv
                else:
                    telephony = TelephonyConnection.objects.select_for_update().get(pk=connection_id)
                    if not telephony.is_active or telephony.organization_id != organization_id or telephony.external_id != account:
                        raise ValueError("source_scope_changed")
                    source = PhoneCall.objects.select_for_update().get(pk=source_id)
                if source.intake_scope:
                    actual = source_scope(source_type, source, locked, telephony if source_type == "call" else None)
                    if actual != source.intake_scope:
                        raise ValueError("source_scope_changed")
                capture_source(source_type, source)
                cursor.last_id, cursor.error_code = source_id, ""
                cursor.save()
                checked += 1
        except Exception:
            # The failed row and cursor move rolled back. Preserve a safe gap code.
            failed_cursor, _ = CrmInboxCursor.objects.get_or_create(**scope)
            CrmInboxCursor.objects.filter(pk=failed_cursor.pk).update(error_code="reconcile_failed")
            return {"checked": checked, "error_code": "reconcile_failed"}
    return {"checked": checked, "cycle_complete": False}


def inbox_status(*, organization_id, source_type, connection_id):
    # Validate fresh organization/channel scope before returning scoped counts.
    _connection(organization_id, source_type, connection_id)
    now = timezone.now()
    rows = CrmInboxEvent.objects.filter(organization_id=organization_id, source_type=source_type,
        connection_id_snapshot=connection_id)
    counts = {row["state"]: row["total"] for row in rows.values("state").annotate(total=Count("pk"))}
    oldest = rows.exclude(state__in=["done", "superseded"]).aggregate(value=Min("received_at"))["value"]
    return {"counts": counts,
        "due": rows.filter(state__in=["pending", "retry", "awaiting_consumer"], next_attempt_at__lte=now).count(),
        "stale_claims": rows.filter(state="claimed", lease_until__lte=now).count(),
        "oldest_age_seconds": max(0, int((now - oldest).total_seconds())) if oldest else None,
        "last_delivery_at": rows.aggregate(value=Max("processed_at"))["value"],
        "last_prepared_at": rows.aggregate(value=Max("prepared_at"))["value"],
        "process_enabled": Organization.objects.get(pk=organization_id).crm_intake_process,
        "consumer_available": True,
        "cursors": list(CrmInboxCursor.objects.filter(organization_id=organization_id, source_type=source_type,
            connection_id_snapshot=connection_id).values("after_id", "requested_through_id", "through_id", "last_id", "cycles", "include_legacy", "error_code"))}
