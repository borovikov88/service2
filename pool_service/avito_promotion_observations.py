"""Scoped observations of provider promotion state; never a purchase ledger.

This helper consumes an already-normalized manual Item API response. It makes
no provider request and does not infer purchase/start dates or spend.
"""
from datetime import datetime, timedelta, timezone as utc_timezone
import hashlib
import json
import re

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .avito_feed_models import AvitoServicesFeed
from .avito_services_history import record_version
from .avito_services_workplace_setup import _binding
from .avito_workspace import ITEM_STATUSES
from .communication_models import ChannelConnection
from .communication_services import organization_access, conversation_capability

KIND = "provider_promotion_observation"
FIELD = "AvitoPromotionState"
SOURCE = "core_v1_account_item"
CONNECTION_ID = 4
ACCOUNT_ID = "446312402"


class PromotionObservationError(ValueError):
    """Only fixed safe codes; callers must not expose a raw provider response."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def _date(value):
    try:
        parsed = value if isinstance(value, datetime) else parse_datetime(value) if isinstance(value, str) else None
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed.astimezone(utc_timezone.utc) if parsed is not None and timezone.is_aware(parsed) else None


def _current(connection_id, *, locked=False):
    query = ChannelConnection.objects.select_related("channel", "avito_credential")
    if locked:
        query = query.select_for_update()
    current = query.filter(pk=connection_id).first()
    if (current is None or current.pk != CONNECTION_ID
            or str(current.external_id) != ACCOUNT_ID
            or not current.is_active or not current.channel.is_active
            or current.channel.kind != "avito"):
        raise PromotionObservationError("promotion_observation_scope_invalid")
    binding = _binding(current)
    if not binding.get("credential_stamp"):
        raise PromotionObservationError("promotion_observation_scope_invalid")
    return current, binding


def capture_binding(connection):
    """Capture BEFORE the existing provider request, never after it."""
    _, binding = _current(getattr(connection, "pk", None))
    return dict(binding)


def _actor(actor, organization):
    current = get_user_model().objects.filter(pk=getattr(actor, "pk", None), is_active=True).first()
    if (current is None or organization_access(current, organization) is None
            or not conversation_capability(current, "can_manage_channels", organization)):
        raise PermissionDenied
    return current


def _state(data):
    # fetch_item_detail must preserve the provider field's presence. Its old
    # absent/null -> [] normalization cannot prove that no promotion exists.
    if data.get("vas_available") is not True:
        return None
    services = data.get("vas")
    if not isinstance(services, list) or len(services) > 100:
        raise PromotionObservationError("promotion_observation_data_invalid")
    result = []
    for service in services:
        if (not isinstance(service, dict) or not isinstance(service.get("id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", service["id"])):
            raise PromotionObservationError("promotion_observation_data_invalid")
        schedule = service.get("schedule")
        if not isinstance(schedule, list) or len(schedule) > 50:
            raise PromotionObservationError("promotion_observation_data_invalid")
        finish = _date(service.get("finish_time"))
        dates = [_date(value) for value in schedule]
        result.append({
            "id": service["id"],
            "finish_time": finish.isoformat() if finish else None,
            "finish_time_known": finish is not None,
            "schedule": sorted({value.isoformat() for value in dates if value is not None}),
            "schedule_complete": bool(dates) and all(value is not None for value in dates),
        })
    # Order/duplicate rows are not evidence of an additional service purchase.
    unique = {_digest(row): row for row in result}
    state = {"services": sorted(unique.values(), key=lambda row: (row["id"], _digest(row)))}
    if len(json.dumps(state, ensure_ascii=False).encode()) > 64 * 1024:
        raise PromotionObservationError("promotion_observation_limit")
    return state


def _previous(feed, *, avito_id, item_id, scope, binding_hash):
    # This helper creates exactly one typed change per verify event. JSON key
    # lookups work on SQLite/MySQL; no JSON-contains or full snapshot transfer.
    event = feed.versions.filter(action="verify",
        changes__0__kind=KIND, changes__0__source=SOURCE,
        changes__0__avito_id=avito_id, changes__0__binding_sha256=binding_hash,
        changes__0__scope__connection_id=scope["connection_id"],
        changes__0__scope__account_id=scope["account_id"],
        changes__0__scope__organization_id=scope["organization_id"],
    ).order_by("-pk").values("pk", "changes").first()
    if event is None:
        return None
    changes = event["changes"]
    if (not isinstance(changes, list) or len(changes) != 1
            or len(json.dumps(changes, ensure_ascii=False).encode()) > 64 * 1024):
        raise PromotionObservationError("promotion_observation_history_invalid")
    change = changes[0]
    observed = _date(change.get("observed_at"))
    state = change.get("after")
    if (change.get("schema_version") != 1 or change.get("field") != FIELD
            or change.get("id") != item_id or change.get("scope") != scope
            or observed is None or not isinstance(state, dict) or set(state) != {"services"}
            or not isinstance(state.get("services"), list) or len(state["services"]) > 100):
        raise PromotionObservationError("promotion_observation_history_invalid")
    for service in state["services"]:
        if (not isinstance(service, dict) or set(service) != {
                "id", "finish_time", "finish_time_known", "schedule", "schedule_complete"}
                or not isinstance(service["id"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", service["id"])
                or type(service["finish_time_known"]) is not bool
                or type(service["schedule_complete"]) is not bool
                or not isinstance(service["schedule"], list) or len(service["schedule"]) > 50
                or any(_date(value) is None for value in service["schedule"])
                or (service["finish_time_known"] and _date(service["finish_time"]) is None)
                or (not service["finish_time_known"] and service["finish_time"] is not None)):
            raise PromotionObservationError("promotion_observation_history_invalid")
    return {"version_id": event["pk"], "observed_at": observed, "state": state}


def observe(connection, actor, data, checked_at, source_binding, *, expected_item_id):
    """Append an observation after the ordinary manual read; no external I/O.

    A baseline/changed observed state is not proof of purchase or its start time.
    Repeated identical state is deduplicated under the connection/feed locks.
    Return None when the provider did not explicitly supply its VAS field;
    fixed ValueError codes/PermissionDenied refuse invalid evidence or access.
    """
    identifier = str(expected_item_id) if type(expected_item_id) in (str, int) else ""
    if (not re.fullmatch(r"[1-9][0-9]{0,31}", identifier)
            or not isinstance(data, dict) or data.get("item_id") != identifier
            or not isinstance(data.get("status"), str) or data["status"] not in ITEM_STATUSES):
        raise PromotionObservationError("promotion_observation_data_invalid")
    observed, now = _date(checked_at), timezone.now()
    if (observed is None or observed > now + timedelta(seconds=30)
            or now - observed > timedelta(minutes=10)):
        raise PromotionObservationError("promotion_observation_time_invalid")
    if not isinstance(source_binding, dict):
        raise PromotionObservationError("promotion_observation_binding_changed")
    state = _state(data)
    with transaction.atomic():
        current, binding = _current(getattr(connection, "pk", None), locked=True)
        if binding != source_binding:
            raise PromotionObservationError("promotion_observation_binding_changed")
        actor = _actor(actor, current.channel.organization)
        feed = AvitoServicesFeed.objects.select_for_update().filter(
            connection=current, account_id=ACCOUNT_ID,
            organization_id_at_import=current.channel.organization_id).first()
        if feed is None:
            raise PromotionObservationError("promotion_observation_scope_invalid")
        from .avito_services_feed import entries
        try:
            draft_rows, published_rows = entries(feed.draft), entries(feed.published)
            if len(draft_rows) > 200 or len(published_rows) > 200 or any(
                    not isinstance(row, dict) for row in draft_rows + published_rows):
                raise ValueError
        except (TypeError, KeyError, ValueError):
            raise PromotionObservationError("promotion_observation_scope_invalid") from None
        rows = [row for row in draft_rows if row.get("AvitoId") == identifier]
        if len(rows) != 1 or not isinstance(rows[0].get("Id"), str) or not 0 < len(rows[0]["Id"]) <= 100:
            raise PromotionObservationError("promotion_observation_item_unverified")
        item_id = rows[0]["Id"]
        published = [row for row in published_rows if row.get("AvitoId") == identifier]
        if (len(published) > 1 or published and published[0].get("Id") != item_id
                or data.get("autoload_item_id") not in (None, "", item_id)):
            raise PromotionObservationError("promotion_observation_item_unverified")
        if state is None:
            return None
        scope = {"connection_id": current.pk, "account_id": ACCOUNT_ID,
                 "organization_id": current.channel.organization_id}
        binding_hash = _digest(binding)
        previous = _previous(feed, avito_id=identifier, item_id=item_id,
                             scope=scope, binding_hash=binding_hash)
        if previous and previous["state"] == state:
            return {"state": "unchanged", "code": "", "version_id": previous["version_id"],
                    "purchase_confirmed": False}
        if previous and observed <= previous["observed_at"]:
            raise PromotionObservationError("promotion_observation_stale")
        change = {"schema_version": 1, "kind": KIND, "source": SOURCE, "scope": scope,
                  "binding_sha256": binding_hash, "avito_id": identifier, "id": item_id,
                  "field": FIELD, "before": previous["state"] if previous else None, "after": state,
                  "observed_at": observed.isoformat(), "time_source": "service2_observed_at",
                  "provider_item_status": data["status"], "baseline": previous is None,
                  "purchase_confirmed": False, "promotion_started_at": None,
                  "promotion_started_at_source": "unknown"}
        event = record_version(feed, "verify", feed.draft, feed.draft, actor=actor,
            reason="Наблюдение услуг продвижения через API; покупка и дата начала не подтверждены",
            extra_changes=[change])
        return {"state": "recorded", "code": "", "version_id": event.pk,
                "baseline": previous is None, "purchase_confirmed": False}
