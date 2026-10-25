"""Scoped Avito outbox. Provider I/O never occurs under a database transaction."""
import hashlib
import json
import math
import re
import uuid
from datetime import timedelta

from django.db import connection as database, transaction
from django.db.models import Min
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from pool_service.communication_avito import (
    AvitoAmbiguousDeliveryError, AvitoError, AvitoRetryableError,
    _json_list_request, _json_request, _provider_root, access_token, send_message,
)
from pool_service.communication_models import (
    AvitoCredential, ChannelConnection, CommunicationChannel, ConversationMessage,
)
from pool_service.communication_services import conversation_capability
from urllib.parse import quote

KEY = "avito_outbox_v1"
MAX_ATTEMPTS = 3
LEASE_SECONDS = 180
SAFE_ERRORS = {
    "provider_unavailable", "provider_delivery_unknown", "provider_account_mismatch",
    "provider_credentials_missing", "provider_credentials_invalid", "provider_token_missing",
    "provider_reconcile_incomplete", "provider_reconcile_invalid", "provider_reconcile_ambiguous",
    "attachments_require_provider_upload", "empty_message",
    "outbox_scope_invalid", "outbox_sender_access_revoked", "outbox_internal_error",
    "outbox_legacy_held", "outbox_transaction_open", "outbox_binding_changed", "outbox_worker_busy",
}


def safe_error(value):
    value = str(value or "")
    if value in SAFE_ERRORS or re.fullmatch(r"provider_http_[1-5][0-9]{2}", value):
        return value
    return "outbox_internal_error" if value else ""


def _date(value):
    try:
        result = parse_datetime(value) if isinstance(value, str) else None
        return result if result and timezone.is_aware(result) else None
    except (ValueError, TypeError):
        return None


def _scope(connection):
    return bool(
        connection.is_active and connection.channel.is_active
        and connection.channel.kind == CommunicationChannel.KIND_AVITO
        and (connection.pk != 4 or str(connection.external_id) == "446312402")
    )


def _metadata(connection):
    value = connection.settings.get(KEY, {}) if isinstance(connection.settings, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _bound(connection, metadata):
    return bool(
        _scope(connection)
        and str(metadata.get("account_id")) == str(connection.external_id)
        and metadata.get("organization_id") == connection.channel.organization_id
    )


def _set_metadata(connection, metadata):
    connection.settings = {**(connection.settings if isinstance(connection.settings, dict) else {}), KEY: metadata}
    connection.save(update_fields=["settings"])


def _locked_connection(connection_id):
    # One lock order for reply save, worker and configuration.
    reference = ChannelConnection.objects.only("channel_id").get(pk=connection_id)
    channel = CommunicationChannel.objects.select_for_update().get(pk=reference.channel_id)
    connection = ChannelConnection.objects.select_for_update().get(pk=connection_id)
    connection.channel = channel
    return connection


def configure_outbox(connection_id, account_id):
    """Explicitly enable future replies only; preserve an existing cutoff."""
    connection = ChannelConnection.objects.select_related("channel").get(pk=connection_id)
    if not _scope(connection) or str(account_id) != str(connection.external_id):
        raise AvitoError("outbox_scope_invalid")
    _provider_context(connection)
    with transaction.atomic():
        connection = _locked_connection(connection_id)
        if not _scope(connection) or str(account_id) != str(connection.external_id):
            raise AvitoError("outbox_scope_invalid")
        metadata = _metadata(connection)
        if metadata and not _bound(connection, metadata):
            raise AvitoError("outbox_binding_changed")
        metadata.update(
            enabled=bool(metadata.get("enabled")) if metadata else True, account_id=str(connection.external_id),
            organization_id=connection.channel.organization_id,
            enabled_at=metadata.get("enabled_at") or timezone.now().isoformat(),
        )
        _set_metadata(connection, metadata)
    return outbox_diagnostics(connection)


def register_reply_intent(message):
    """Called in the existing reply-save transaction; no provider I/O."""
    connection = message.conversation.connection
    if connection.pk != 4 or not _scope(connection):
        return False
    metadata = _metadata(connection)
    if not metadata:
        # Only this newly created manager reply and subsequent replies qualify.
        _set_metadata(connection, {
            "enabled": True, "account_id": str(connection.external_id),
            "organization_id": connection.channel.organization_id,
            "enabled_at": message.created_at.isoformat(),
        })
        return True
    return bool(_bound(connection, metadata) and metadata.get("enabled"))


def _provider_context(connection):
    if database.in_atomic_block:
        raise AvitoError("outbox_transaction_open")
    token = access_token(connection)
    response = _json_request(
        _provider_root() + "/core/v1/accounts/self",
        headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
        timeout=5,
    )
    account = response.get("id")
    if isinstance(account, bool) or str(account) != str(connection.external_id):
        raise AvitoError("provider_account_mismatch")
    return token


def _snapshot(message):
    try:
        credential = message.conversation.connection.avito_credential
        credential_key = [credential.client_id_encrypted, credential.client_secret_encrypted]
    except AvitoCredential.DoesNotExist:
        credential_key = None
    value = [
        message.conversation.connection_id, message.conversation.organization_id,
        message.conversation.connection.channel_id,
        message.conversation.connection.channel.organization_id,
        str(message.conversation.connection.external_id),
        message.conversation.external_id, message.body, message.sent_by_id, credential_key,
    ]
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def _message(connection_id, message_id):
    message = ConversationMessage.objects.select_related(
        "conversation__connection__channel", "conversation__connection__avito_credential", "sent_by",
    ).get(pk=message_id, conversation__connection_id=connection_id,
          direction=ConversationMessage.DIRECTION_OUT)
    connection = message.conversation.connection
    if not _scope(connection) or message.conversation.organization_id != connection.channel.organization_id:
        raise AvitoError("outbox_scope_invalid")
    return message


def reconcile_message(connection_id, message_id):
    """Read only. Absence requires complete bounded history, never a partial page."""
    result = {"state": "unknown", "error_code": "", "complete": False,
              "pages": 0, "messages_checked": 0, "provider_id": None}
    try:
        message = _message(connection_id, message_id)
        token = _provider_context(message.conversation.connection)
        return _reconcile(message, token, result)
    except (AvitoError, ConversationMessage.DoesNotExist, ChannelConnection.DoesNotExist) as error:
        result["error_code"] = safe_error(error) if isinstance(error, AvitoError) else "outbox_scope_invalid"
        return result


def _reconcile(message, token, result=None):
    result = result or {"state": "unknown", "error_code": "", "complete": False,
                        "pages": 0, "messages_checked": 0, "provider_id": None}
    account = str(message.conversation.connection.external_id)
    url = (_provider_root() + "/messenger/v3/accounts/" + quote(account, safe="")
           + "/chats/" + quote(message.conversation.external_id, safe="") + "/messages/")
    matches = set()
    earlier_match = False
    seen = set()
    for page in range(5):
        values = _json_list_request(url + "?limit=100&offset=" + str(page * 100),
            headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, timeout=5)
        result["pages"] += 1
        if not isinstance(values, list) or len(values) > 100:
            result["error_code"] = "provider_reconcile_invalid"
            return result
        for value in values:
            result["messages_checked"] += 1
            if not isinstance(value, dict):
                result["error_code"] = "provider_reconcile_invalid"
                return result
            provider_id, created = value.get("id"), value.get("created")
            direction, author = value.get("direction"), value.get("author_id")
            if (not isinstance(provider_id, str) or not provider_id or len(provider_id) > 255
                    or provider_id in seen or isinstance(created, bool)
                    or not isinstance(created, (int, float)) or not math.isfinite(created) or created <= 0
                    or provider_id != provider_id.strip()
                    or (direction not in {"in", "out"} and author is None)):
                result["error_code"] = "provider_reconcile_invalid"
                return result
            seen.add(provider_id)
            outbound = direction == "out" or (direction != "in" and str(author) == account)
            if direction == "out" and author is not None and str(author) != account:
                result["error_code"] = "provider_reconcile_invalid"
                return result
            content = value.get("content")
            exact = value.get("type") == "text" and isinstance(content, dict) and content.get("text") == message.body
            in_window = message.created_at.timestamp() - 300 <= created <= timezone.now().timestamp() + 60
            if outbound and exact and not message.external_id and in_window and created < message.created_at.timestamp():
                earlier_match = True
            elif outbound and exact and (provider_id == message.external_id or in_window):
                matches.add(provider_id)
        if len(values) < 100:
            result["complete"] = True
            break
    if not result["complete"]:
        result["error_code"] = "provider_reconcile_incomplete"
    elif message.external_id:
        if message.external_id in matches:
            result.update(state="accepted", provider_id=message.external_id)
        else:
            result["error_code"] = "provider_delivery_unknown"
    elif earlier_match:
        result["error_code"] = "provider_reconcile_ambiguous"
    elif len(matches) == 1:
        result.update(state="accepted", provider_id=next(iter(matches)))
    elif len(matches) > 1:
        result["error_code"] = "provider_reconcile_ambiguous"
    else:
        result["state"] = "absent"
    return result


def _actor_allowed(message):
    return bool(message.sent_by_id and message.sent_by.is_active
                and conversation_capability(message.sent_by, "can_reply_conversations",
                                            message.conversation.organization))


def _eligible(message, metadata):
    cutoff = _date(metadata.get("enabled_at"))
    return bool(cutoff and message.created_at >= cutoff and _actor_allowed(message))


def claim_message(message_id, *, connection_id, expected_snapshot=None, allow_legacy=False):
    with transaction.atomic():
        connection = _locked_connection(connection_id)
        metadata = _metadata(connection)
        message = ConversationMessage.objects.select_for_update().select_related(
            "conversation__connection__channel", "conversation__connection__avito_credential", "sent_by",
        ).filter(pk=message_id, conversation__connection=connection,
                 direction=ConversationMessage.DIRECTION_OUT,
                 delivery_status=ConversationMessage.DELIVERY_PENDING, external_id__isnull=True).first()
        if message is None or not _bound(connection, metadata) or not metadata.get("enabled"):
            return None
        if message.conversation.organization_id != connection.channel.organization_id or not _actor_allowed(message):
            return None
        if not allow_legacy and not _eligible(message, metadata):
            return None
        if expected_snapshot is not None and _snapshot(message) != expected_snapshot:
            return None
        if message.attachments.exists() or message.delivery_attempts >= MAX_ATTEMPTS:
            return None
        message.delivery_status = ConversationMessage.DELIVERY_SENDING
        message.delivery_attempts += 1
        message.delivery_error = ""
        message.save(update_fields=["delivery_status", "delivery_attempts", "delivery_error"])
        return message


def _record(connection_id, message_id, *, status, error="", provider_id=None, expected_snapshot=None, expected_guard=None):
    with transaction.atomic():
        connection = _locked_connection(connection_id)
        message = ConversationMessage.objects.select_for_update().select_related(
            "conversation__connection__channel", "conversation__connection__avito_credential", "sent_by",
        ).filter(pk=message_id, conversation__connection=connection,
                 conversation__organization_id=connection.channel.organization_id,
                 direction=ConversationMessage.DIRECTION_OUT).first()
        if message is None or not _scope(connection):
            return False
        if expected_snapshot is not None and _snapshot(message) != expected_snapshot:
            return False
        if expected_guard is not None and not expected_guard(message):
            return False
        if message.delivery_status == ConversationMessage.DELIVERY_DELIVERED and message.external_id:
            return provider_id is None or provider_id == message.external_id
        message.delivery_status, message.delivery_error = status, safe_error(error)
        fields = ["delivery_status", "delivery_error"]
        if provider_id:
            message.external_id, message.delivered_at = provider_id, timezone.now()
            fields += ["external_id", "delivered_at"]
        message.save(update_fields=fields)
        return True


def message_snapshot(message):
    return _snapshot(message)


def record_reconciled_message(connection_id, message_id, provider_id, *, expected_snapshot, expected_guard=None):
    if (not isinstance(provider_id, str) or not provider_id.strip()
            or provider_id != provider_id.strip() or len(provider_id) > 255):
        return False
    return _record(connection_id, message_id, status=ConversationMessage.DELIVERY_DELIVERED,
                   provider_id=provider_id, expected_snapshot=expected_snapshot, expected_guard=expected_guard)


def _queue_counts(connection):
    queue = ConversationMessage.objects.filter(
        conversation__connection=connection,
        conversation__organization_id=connection.channel.organization_id,
        direction=ConversationMessage.DIRECTION_OUT)
    return {key: queue.filter(delivery_status=status).count() for key, status in (
        ("pending", ConversationMessage.DELIVERY_PENDING),
        ("unknown", ConversationMessage.DELIVERY_SENDING),
        ("failed", ConversationMessage.DELIVERY_FAILED),
    )}


def _start(connection_id, periodic=False, immediate=False):
    token = str(uuid.uuid4())
    now = timezone.now()
    with transaction.atomic():
        connection = _locked_connection(connection_id)
        metadata = _metadata(connection)
        if not _bound(connection, metadata) or not metadata.get("enabled"):
            return None
        # Worker tokens are health evidence, not a connection-wide send lock.
        # Different explicit new replies must not strand one another. The
        # message-row claim is the authoritative single-flight send guard.
        workers = metadata.get("workers") if isinstance(metadata.get("workers"), dict) else {}
        workers = {key: value for key, value in workers.items()
                   if isinstance(key, str) and _date(value) and _date(value) > now}
        if len(workers) >= 100:
            metadata["last_error_code"] = "outbox_worker_busy"
            _set_metadata(connection, metadata)
            return None
        until = (now + timedelta(seconds=LEASE_SECONDS)).isoformat()
        workers[token] = until
        metadata.update(workers=workers, lease_until=max(workers.values(), key=_date),
                        last_started_at=now.isoformat(), last_error_code="")
        modes = metadata.get("worker_modes") if isinstance(metadata.get("worker_modes"), dict) else {}
        mode = "periodic" if periodic else "immediate" if immediate else "manual"
        metadata["worker_modes"] = {key: value for key, value in modes.items() if key in workers}
        metadata["worker_modes"][token] = mode
        if mode in {"periodic", "immediate"}:
            metadata["last_" + mode + "_run_token"] = token
        if periodic:
            metadata["last_periodic_started_at"] = now.isoformat()
            history = metadata.get("periodic_runs") if isinstance(metadata.get("periodic_runs"), list) else []
            metadata["periodic_runs"] = history[-3:] + [{
                "token": token, "started_at": now.isoformat(), "completed_at": None,
                "error_code": "", "result": None, "queue_before": _queue_counts(connection), "queue_after": None,
            }]
        elif immediate:
            metadata["last_immediate_started_at"] = now.isoformat()
        _set_metadata(connection, metadata)
    return token


def _finish(connection_id, token, error="", periodic=False, immediate=False, result=None):
    with transaction.atomic():
        connection = _locked_connection(connection_id)
        metadata = _metadata(connection)
        workers = metadata.get("workers") if isinstance(metadata.get("workers"), dict) else {}
        if token not in workers:
            return
        now = timezone.now()
        workers = {key: value for key, value in workers.items()
                   if key != token and _date(value) and _date(value) > now}
        metadata.update(workers=workers, lease_until=max(workers.values(), key=_date) if workers else None,
                        last_error_code=safe_error(error))
        modes = metadata.get("worker_modes") if isinstance(metadata.get("worker_modes"), dict) else {}
        metadata["worker_modes"] = {key: value for key, value in modes.items() if key in workers}
        prefix = "periodic" if periodic else "immediate" if immediate else None
        if prefix and metadata.get("last_" + prefix + "_run_token") == token:
            metadata["last_" + prefix + "_completed_at"] = now.isoformat()
            metadata["last_" + prefix + "_error_code"] = safe_error(error)
        if periodic:
            history = metadata.get("periodic_runs") if isinstance(metadata.get("periodic_runs"), list) else []
            metadata["periodic_runs"] = [
                {**row, "completed_at": now.isoformat(), "error_code": safe_error(error),
                 "queue_after": _queue_counts(connection), "result": {key: result[key] for key in ("accepted", "pending", "failed", "unknown", "held")}}
                if isinstance(row, dict) and row.get("token") == token and isinstance(result, dict) else row
                for row in history[-4:]
            ]
        if not error:
            metadata["last_success_at"] = now.isoformat()
            if prefix:
                metadata["last_" + prefix + "_success_at"] = metadata["last_success_at"]
        _set_metadata(connection, metadata)


def run_outbox(connection_id, *, message_id=None, limit=10, explicit=False, sender=None,
               expected_guard=None, periodic=False, immediate=False):
    """Only future manager replies, or one explicitly selected legacy row."""
    if database.in_atomic_block:
        raise AvitoError("outbox_transaction_open")
    lease = _start(connection_id, periodic=periodic, immediate=immediate)
    result = {"accepted": 0, "pending": 0, "failed": 0, "unknown": 0, "held": 0}
    if not lease:
        result["held"] = 1
        return result
    last_error = ""
    sender = sender or send_message
    try:
        connection = ChannelConnection.objects.select_related("channel").get(pk=connection_id)
        metadata = _metadata(connection)
        if message_id is not None:
            ids = [message_id]
        else:
            cutoff = _date(metadata.get("enabled_at"))
            ids = list(ConversationMessage.objects.filter(
                conversation__connection_id=connection_id, conversation__organization_id=connection.channel.organization_id,
                direction=ConversationMessage.DIRECTION_OUT, delivery_status=ConversationMessage.DELIVERY_PENDING,
                created_at__gte=cutoff, sent_by__isnull=False,
            ).order_by("created_at", "pk").values_list("pk", flat=True)[:max(1, min(limit, 50))]) if cutoff else []
        for selected_id in ids:
            claimed = False
            before = None
            try:
                message = _message(connection_id, selected_id)
                if not _actor_allowed(message):
                    last_error = "outbox_sender_access_revoked"
                    status = (ConversationMessage.DELIVERY_SENDING if message.delivery_status == ConversationMessage.DELIVERY_SENDING
                              else ConversationMessage.DELIVERY_FAILED)
                    _record(connection_id, selected_id, status=status,
                            error=last_error, expected_snapshot=_snapshot(message))
                    result["unknown" if status == ConversationMessage.DELIVERY_SENDING else "failed"] += 1
                    continue
                if not explicit and (not _eligible(message, metadata)
                                     or message.delivery_status != ConversationMessage.DELIVERY_PENDING):
                    result["held"] += 1
                    continue
                if expected_guard is not None and not expected_guard(message):
                    result["held"] += 1
                    last_error = "outbox_binding_changed"
                    continue
                before = _snapshot(message)
                may_send = message.delivery_status == ConversationMessage.DELIVERY_PENDING and (
                    not explicit or message.delivery_attempts == 0
                ) and not message.external_id
                if may_send:
                    message = claim_message(selected_id, connection_id=connection_id,
                                            expected_snapshot=before, allow_legacy=explicit)
                    if message is None:
                        result["held"] += 1
                        continue
                    claimed = True
                token = _provider_context(message.conversation.connection)
                # Fresh durable manager intents have never begun POST. A body
                # match to an earlier independent reply is not their receipt.
                reconciliation = (_reconcile(message, token) if explicit else
                                  {"state": "absent", "error_code": "", "provider_id": None})
                if reconciliation["state"] == "accepted":
                    if expected_guard is not None and not expected_guard(_message(connection_id, selected_id)):
                        raise AvitoError("outbox_binding_changed")
                    recorded = _record(connection_id, selected_id,
                        status=ConversationMessage.DELIVERY_DELIVERED,
                        provider_id=reconciliation["provider_id"], expected_snapshot=before, expected_guard=expected_guard)
                    result["accepted" if recorded else "held"] += 1
                    continue
                if reconciliation["state"] != "absent" or not claimed:
                    last_error = reconciliation["error_code"] or "provider_delivery_unknown"
                    _record(connection_id, selected_id, status=ConversationMessage.DELIVERY_SENDING,
                            error=last_error, expected_snapshot=before)
                    result["unknown"] += 1
                    continue
                # Re-check the current context after GET and directly before POST.
                current = _message(connection_id, selected_id)
                if (_snapshot(current) != before or not _actor_allowed(current)
                        or (expected_guard is not None and not expected_guard(current))
                        or current.delivery_status != ConversationMessage.DELIVERY_SENDING):
                    raise AvitoError("outbox_binding_changed")
                if database.in_atomic_block:
                    raise AvitoError("outbox_transaction_open")
                sender(current, token=token)
                current.refresh_from_db()
                if current.delivery_status == ConversationMessage.DELIVERY_DELIVERED and current.external_id:
                    result["accepted"] += 1
                else:
                    last_error = "provider_delivery_unknown"
                    _record(connection_id, selected_id, status=ConversationMessage.DELIVERY_SENDING,
                            error=last_error, expected_snapshot=before)
                    result["unknown"] += 1
            except AvitoAmbiguousDeliveryError as error:
                last_error = safe_error(error)
                if before:
                    _record(connection_id, selected_id, status=ConversationMessage.DELIVERY_SENDING,
                            error=last_error, expected_snapshot=before)
                result["unknown"] += 1
            except AvitoRetryableError as error:
                last_error = safe_error(error)
                if claimed:
                    current = _message(connection_id, selected_id)
                    status = (ConversationMessage.DELIVERY_PENDING if current.delivery_attempts < MAX_ATTEMPTS
                              else ConversationMessage.DELIVERY_FAILED)
                    _record(connection_id, selected_id, status=status, error=last_error, expected_snapshot=before)
                    result["pending" if status == ConversationMessage.DELIVERY_PENDING else "failed"] += 1
                else:
                    result["held"] += 1
            except (AvitoError, ConversationMessage.DoesNotExist, ChannelConnection.DoesNotExist) as error:
                last_error = safe_error(error) if isinstance(error, AvitoError) else "outbox_scope_invalid"
                if before:
                    status = (ConversationMessage.DELIVERY_FAILED if claimed else
                              ConversationMessage.DELIVERY_SENDING)
                    _record(connection_id, selected_id, status=status,
                            error=last_error, expected_snapshot=before)
                result["failed" if claimed else "unknown"] += 1
            except Exception:
                # An unexpected failure can occur after POST. Never requeue it blindly.
                last_error = "provider_delivery_unknown"
                if before:
                    _record(connection_id, selected_id, status=ConversationMessage.DELIVERY_SENDING,
                            error=last_error, expected_snapshot=before)
                result["unknown"] += 1
    except Exception:
        # Failure before selecting a row must not be recorded as worker success.
        last_error = "outbox_internal_error"
        raise
    finally:
        _finish(connection_id, lease, last_error, periodic=periodic, immediate=immediate, result=result)
    return result


def dispatch_committed_reply(connection_id, message_id):
    """Best effort immediate dispatch after commit; periodic processing is the fallback."""
    try:
        return run_outbox(connection_id, message_id=message_id, immediate=True)
    except Exception:
        # The reply is already durable. Never turn a committed save into an HTTP500.
        return {"unknown": 1}


def _mechanism_state(metadata, mode, enabled, now):
    if not enabled:
        return "disabled"
    workers = metadata.get("workers") if isinstance(metadata.get("workers"), dict) else {}
    modes = metadata.get("worker_modes") if isinstance(metadata.get("worker_modes"), dict) else {}
    if any(modes.get(key) == mode and _date(until) and _date(until) > now
           for key, until in workers.items()):
        return "running"
    if safe_error(metadata.get("last_" + mode + "_error_code")):
        return "error"
    started = _date(metadata.get("last_" + mode + "_started_at"))
    success = _date(metadata.get("last_" + mode + "_success_at"))
    if not started:
        return "never_run"
    completed = _date(metadata.get("last_" + mode + "_completed_at"))
    if not success or not completed or completed < started:
        return "unknown"
    # Immediate dispatch is event-driven. An idle period is not an outage.
    if mode == "periodic" and now - success > timedelta(minutes=3):
        return "stale"
    return "healthy"


def _periodic_history(metadata, now):
    history = metadata.get("periodic_runs")
    if not isinstance(history, list):
        return []
    result = []
    for row in history[-4:]:
        if not isinstance(row, dict):
            continue
        started, completed = _date(row.get("started_at")), _date(row.get("completed_at"))
        counts = row.get("result")
        if not started or (completed and completed < started):
            continue
        valid_counts = (isinstance(counts, dict) and all(
            type(counts.get(key)) is int and 0 <= counts[key] <= 50
            for key in ("accepted", "pending", "failed", "unknown", "held")))
        queue_before, queue_after = row.get("queue_before"), row.get("queue_after")
        valid_queue = lambda value: isinstance(value, dict) and all(
            type(value.get(key)) is int and 0 <= value[key] <= 10**7
            for key in ("pending", "unknown", "failed"))
        if not valid_queue(queue_before) or (completed and (not valid_counts or not valid_queue(queue_after))):
            continue
        code = safe_error(row.get("error_code"))
        state = ("error" if code else "success") if completed else (
            "running" if now - started <= timedelta(seconds=LEASE_SECONDS) else "unknown")
        result.append({"started_at": started.isoformat(),
                       "completed_at": completed.isoformat() if completed else None,
                       "state": state, "error_code": code,
                       "queue_before": {key: queue_before[key] for key in ("pending", "unknown", "failed")},
                       "queue_after": {key: queue_after[key] for key in ("pending", "unknown", "failed")} if completed else None,
                       "result": {key: counts[key] for key in ("accepted", "pending", "failed", "unknown", "held")}
                                 if completed else None})
    return result


def outbox_diagnostics(connection, now=None):
    now = now or timezone.now()
    metadata = _metadata(connection)
    bound = _bound(connection, metadata)
    pending = ConversationMessage.objects.filter(
        conversation__connection=connection, conversation__organization_id=connection.channel.organization_id,
        direction=ConversationMessage.DIRECTION_OUT, delivery_status=ConversationMessage.DELIVERY_PENDING,
    )
    oldest = pending.aggregate(value=Min("created_at"))["value"]
    cutoff, started, success, lease = (_date(metadata.get(key)) for key in
        ("enabled_at", "last_started_at", "last_success_at", "lease_until"))
    enabled = bool(bound and metadata.get("enabled"))
    if not enabled:
        state = "disabled"
    elif lease and lease > now:
        state = "running"
    elif metadata.get("last_error_code"):
        state = "error"
    elif not started:
        state = "never_run"
    elif not success or now - success > timedelta(minutes=3):
        state = "stale"
    else:
        state = "healthy"
    queue = ConversationMessage.objects.filter(
        conversation__connection=connection, conversation__organization_id=connection.channel.organization_id,
        direction=ConversationMessage.DIRECTION_OUT)
    return {
        "connection_id": connection.pk, "account_id": str(connection.external_id),
        "enabled": enabled, "binding_valid": _scope(connection) and (not metadata or bound),
        "worker_state": state, "last_started_at": started.isoformat() if started else None,
        "last_success_at": success.isoformat() if success else None,
        "immediate_state": _mechanism_state(metadata, "immediate", enabled, now),
        "periodic_state": _mechanism_state(metadata, "periodic", enabled, now),
        **{key: value.isoformat() if value else None for key in (
            "last_immediate_started_at", "last_immediate_completed_at", "last_immediate_success_at",
            "last_periodic_started_at", "last_periodic_completed_at", "last_periodic_success_at",
        ) for value in [_date(metadata.get(key))]},
        "last_immediate_error_code": safe_error(metadata.get("last_immediate_error_code")),
        "last_periodic_error_code": safe_error(metadata.get("last_periodic_error_code")),
        "periodic_runs": _periodic_history(metadata, now),
        "last_error_code": safe_error(metadata.get("last_error_code")),
        "pending_count": pending.count(), "oldest_pending_at": oldest.isoformat() if oldest else None,
        "oldest_pending_age_seconds": max(0, int((now - oldest).total_seconds())) if oldest else None,
        "unknown_count": queue.filter(delivery_status=ConversationMessage.DELIVERY_SENDING).count(),
        "failed_count": queue.filter(delivery_status=ConversationMessage.DELIVERY_FAILED).count(),
        "legacy_held_count": pending.filter(created_at__lt=cutoff).count() if cutoff else pending.count(),
    }
