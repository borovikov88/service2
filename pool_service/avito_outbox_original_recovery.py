"""One explicitly approved original reply, never a general replay facility."""
import hashlib
import json
import os
import re
import subprocess

from django.db import transaction
from django.utils import timezone

from . import communication_avito
from .avito_outbox_audit import ACCOUNT_ID, CONNECTION_ID, INCIDENT_START, INCIDENT_END, safe_error
from .avito_readonly_audit import binding
from .communication_models import ChannelConnection, ConversationMessage
from .communication_services import conversation_capability
from .models import OrganizationAccess
from scripts.avito_outbox_recovery_receipt import normalize

MARKER = "[avito-outbox-original-20261025]"
LEDGER_KEY = "avito_original_reply_recovery_20261025"
BODY_SHA = "b2ce9cba4b11e0ca3adde8879e22216d17a7e592280b71d69f7020223fe51531"
CONVERSATION_UUID_SHA = "ed6fe5b7564e9750e9999ec171367c45c215771faa76bf11f1c18a8cc01d7db1"


def _authorized_release(expected_sha):
    if os.environ.get("SERVICE2_AVITO_OUTBOX_RECOVERY") != "1" or not re.fullmatch(r"[0-9a-f]{40}", expected_sha or ""):
        raise ValueError("release_not_authorized")
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, timeout=5)
    message = subprocess.run(["git", "show", "-s", "--format=%B", "HEAD"], capture_output=True, timeout=5)
    if head.returncode or message.returncode or head.stdout.decode().strip() != expected_sha or MARKER not in message.stdout.decode():
        raise ValueError("release_not_authorized")


def _permission(message):
    actor = message.sent_by
    organization_id = message.conversation.organization_id
    return bool(actor and actor.is_active
        and OrganizationAccess.objects.filter(user_id=actor.pk, organization_id=organization_id).exists()
        and conversation_capability(actor, "can_reply_conversations", message.conversation.organization))


def _fixed_tuple(message):
    if message is None:
        return False
    conversation = message.conversation
    connection = conversation.connection
    return bool(message.direction == "out" and connection.pk == CONNECTION_ID and connection.external_id == ACCOUNT_ID
        and connection.is_active and connection.channel.is_active and connection.channel.kind == "avito"
        and conversation.organization_id == connection.channel.organization_id
        and INCIDENT_START <= message.created_at < INCIDENT_END and _permission(message)
        and hashlib.sha256(message.body.encode()).hexdigest() == BODY_SHA
        and hashlib.sha256(str(conversation.uuid).encode()).hexdigest() == CONVERSATION_UUID_SHA)


def _message(message_id):
    return ConversationMessage.objects.select_related(
        "conversation__connection__channel", "sent_by").filter(pk=message_id, conversation__connection_id=CONNECTION_ID).first()


def _base():
    return {"schema_version": 1, "source": "services_original_reply_recovery_v1",
        "checked_at": timezone.now().isoformat(), "connection_id": CONNECTION_ID, "account_id": ACCOUNT_ID,
        "candidate_count": 0, "local_id": None, "tuple_matches": False, "outcome": "held",
        "error_code": "", "initial_status": None, "initial_attempts": None, "final_status": None,
        "final_attempts": None, "provider_before_state": "not_checked", "provider_before_complete": False,
        "provider_message_id_present": False, "send_function_invocations": 0, "ledger_reused": False,
        "duplicate_created": False, "client_read_confirmed": False}


def _current(result, message):
    if message is not None:
        result.update(final_status=message.delivery_status, final_attempts=message.delivery_attempts,
                      provider_message_id_present=bool(message.external_id))
    return result


def recover(*, expected_sha):
    _authorized_release(expected_sha)
    from .avito_outbox import reconcile_message, run_outbox, record_reconciled_message, message_snapshot, _record, safe_error as outbox_safe_error
    result = _base()
    connection = ChannelConnection.objects.select_related("channel").filter(
        pk=CONNECTION_ID, external_id=ACCOUNT_ID, channel__kind="avito", is_active=True, channel__is_active=True,
    ).first()
    if connection is None:
        return normalize(result | {"error_code": "scope_invalid"})
    initial_binding = binding(connection)
    candidates = list(ConversationMessage.objects.select_related("conversation__connection__channel", "sent_by").filter(
        conversation__connection_id=CONNECTION_ID, conversation__organization_id=connection.channel.organization_id,
        direction="out", created_at__gte=INCIDENT_START, created_at__lt=INCIDENT_END,
    ).order_by("pk")[:2])
    result["candidate_count"] = len(candidates)
    if len(candidates) != 1:
        return normalize(result | {"error_code": "original_target_not_unique"})
    original = candidates[0]
    result.update(local_id=original.pk, initial_status=original.delivery_status,
                  initial_attempts=original.delivery_attempts, tuple_matches=_fixed_tuple(original))
    if not result["tuple_matches"]:
        return normalize(_current(result | {"error_code": "original_target_mismatch"}, original))
    original_chat = original.conversation.external_id
    original_sender = original.sent_by_id
    original_snapshot = message_snapshot(original)
    digest = hashlib.sha256(json.dumps(initial_binding, separators=(",", ":")).encode()).hexdigest()

    def current_guard(message):
        return bool(_fixed_tuple(message) and message.pk == original.pk
            and message.sent_by_id == original_sender and message.conversation.external_id == original_chat
            and binding(connection) == initial_binding)

    with transaction.atomic():
        locked = ChannelConnection.objects.select_for_update().get(pk=CONNECTION_ID)
        selected = _message(original.pk)
        if selected is None or not current_guard(selected):
            return normalize(_current(result | {"error_code": "original_target_changed"}, selected))
        settings = dict(locked.settings or {})
        previous = settings.get(LEDGER_KEY)
        if previous is not None:
            # Even a crashed operation is not a reason to start another send.
            if (isinstance(previous, dict) and previous.get("binding_digest") == digest
                    and previous.get("local_id") == original.pk and isinstance(previous.get("result"), dict)):
                cached = dict(previous["result"])
                cached["ledger_reused"] = True
                return normalize(_current(cached, selected))
            return normalize(_current(result | {"outcome": "unknown", "error_code": "original_recovery_unknown",
                                               "ledger_reused": True}, selected))
        settings[LEDGER_KEY] = {"version": 1, "state": "running", "local_id": original.pk,
            "binding_digest": digest, "started_at": result["checked_at"]}
        locked.settings = settings
        locked.save(update_fields=["settings"])
    # No provider request is made in an open transaction.
    try:
        proof = reconcile_message(CONNECTION_ID, original.pk)
        result["provider_before_state"] = proof.get("state")
        result["provider_before_complete"] = proof.get("complete") is True
        fresh = _message(original.pk)
        if fresh is None or not current_guard(fresh):
            result.update(outcome="held", error_code="original_target_changed")
        elif proof.get("state") == "accepted" and proof.get("provider_id"):
            # A positive exact provider match is never turned into another POST.
            recorded = record_reconciled_message(CONNECTION_ID, original.pk,
                proof["provider_id"], expected_snapshot=original_snapshot,
                expected_guard=current_guard)
            result.update(outcome="accepted" if recorded else "held",
                          error_code="" if recorded else "original_target_changed")
        elif (proof.get("state") == "absent" and proof.get("complete") is True
                and fresh.delivery_status == "pending" and fresh.delivery_attempts == 0 and not fresh.external_id):
            def approved_sender(message, *, token=None):
                if not current_guard(message):
                    raise communication_avito.AvitoError("outbox_binding_changed")
                result["send_function_invocations"] += 1
                return communication_avito.send_message(message, token=token)
            run_outbox(CONNECTION_ID, message_id=original.pk, explicit=True,
                       expected_guard=current_guard, sender=approved_sender)
        else:
            result.update(outcome="unknown" if proof.get("state") == "unknown" or fresh.delivery_status == "sending" else "held",
                          error_code=safe_error(proof.get("error_code")) or "original_recovery_unconfirmed")
            if proof.get("state") == "unknown":
                # Record the known block without pretending a POST was started.
                # The status predicate runs under the shared recorder's row lock,
                # so a concurrent claim can never be reverted to pending.
                hold_code = outbox_safe_error(proof.get("error_code")) or "provider_delivery_unknown"
                def pending_hold_guard(message):
                    return bool(current_guard(message) and message.delivery_status == "pending"
                        and message.delivery_attempts == 0 and not message.external_id)
                _record(CONNECTION_ID, original.pk, status="pending", error=hold_code,
                    expected_snapshot=original_snapshot, expected_guard=pending_hold_guard)
        final = _message(original.pk)
        _current(result, final)
        if result["outcome"] not in {"accepted"} and result["error_code"] == "":
            if final and final.delivery_status == "delivered" and final.external_id:
                result["outcome"] = "accepted"
            elif final and final.delivery_status == "failed":
                result.update(outcome="failed", error_code=safe_error(final.delivery_error) or "original_recovery_unconfirmed")
            elif final and final.delivery_status == "sending":
                result.update(outcome="unknown", error_code=safe_error(final.delivery_error) or "original_recovery_unknown")
            else:
                result.update(outcome="held", error_code="original_recovery_unconfirmed")
    except Exception:
        # An exception may follow an accepted POST. Never clear/requeue the row.
        result.update(outcome="unknown", error_code="original_recovery_unknown")
        _current(result, _message(original.pk))
    public = normalize(result)
    with transaction.atomic():
        current = ChannelConnection.objects.select_for_update().get(pk=CONNECTION_ID)
        settings = dict(current.settings or {})
        entry = settings.get(LEDGER_KEY)
        if (isinstance(entry, dict) and entry.get("binding_digest") == digest and entry.get("local_id") == original.pk
                and entry.get("state") == "running"):
            settings[LEDGER_KEY] = {**entry, "state": "finished", "finished_at": timezone.now().isoformat(), "result": public}
            current.settings = settings
            current.save(update_fields=["settings"])
    return public
