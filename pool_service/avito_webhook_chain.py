"""Passive scoped proof of real authenticated Avito intake; no provider requests."""
import hashlib
import json
import re

from django.utils import timezone

from .communication_models import ConversationMessage
from .models import Notification

SOURCE = "avito_webhook_chain_v1"
CHAIN_KEY = "chain"
STATE_LABELS = {
    "awaiting_first": "Ожидается первое входящее событие с проверяемой цепочкой.",
    "binding_unverified": "Текущая привязка webhook не подтверждена.",
    "proof_invalid": "Сохранённая цепочка не подтверждена.",
    "persisted_row_missing": "Событие принято, но сохранённое сообщение больше не подтверждено.",
    "accepted_persisted": "Входящее событие принято, диалог и сообщение подтверждены.",
}
OWNER_LABELS = {
    "awaiting_first": "Ожидается первое входящее событие.",
    "not_checked": "Уведомление пока не подтверждено.",
    "direct_recorded": "Уведомление записано в Service2.",
    "quiet_digest_recorded": "Утреннее уведомление записано в Service2.",
    "queued": "Уведомление сохранено в очереди; учитываются тихие часы.",
    "not_recorded": "Запись уведомления в Service2 не подтверждена.",
    "not_eligible": "У владельца сейчас нет доступа к этому диалогу.",
}
QUEUE_ERRORS = {"notification_preferences_disabled", "conversation_access_denied",
                "binding_changed", "deferred_queue_invalid"}


def _window(connection, metadata=None):
    """Bind the proof to the exact existing registration window and owner."""
    from . import avito_services_workplace_setup as setup
    from .communication_avito import AvitoError
    metadata = metadata if isinstance(metadata, dict) else connection.settings
    if (connection.pk != 4 or str(connection.external_id) != "446312402"
            or not isinstance(metadata, dict)):
        return None
    evidence = metadata.get(setup.WEBHOOK_KEY)
    if not isinstance(evidence, dict) or evidence.get("verified") is not True:
        return None
    try:
        binding = setup._binding(connection)
        valid = (setup._same(connection, binding)
                 and all(evidence.get(key) == value for key, value in binding.items())
                 and evidence.get("token_stamp") == setup._token_stamp(connection)
                 and setup._origin(evidence.get("origin")) == evidence.get("origin"))
        actor = setup._actor(connection) if valid else None
    except AvitoError:
        return None
    verified = setup.when(evidence.get("verified_at"))
    if actor is None or verified is None or verified > timezone.now():
        return None
    # A private deterministic generation avoids mutating/re-registering a working
    # callback. Token/credential/owner/scope/window changes invalidate old proof.
    generation = hashlib.sha256(json.dumps(
        {"connection_id": connection.pk, **binding, "actor_id": actor.pk,
         "token_stamp": evidence["token_stamp"], "verified_at": verified.isoformat(),
         "origin": evidence["origin"]}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return evidence, generation, actor, verified


def _message(connection, message_id, conversation_id=None):
    if type(message_id) is not int or not 0 < message_id < 2**63:
        return None
    filters = {"pk": message_id, "conversation__connection_id": connection.pk,
               "conversation__organization_id": connection.channel.organization_id,
               "direction": ConversationMessage.DIRECTION_IN,
               "delivery_status": ConversationMessage.DELIVERY_RECEIVED}
    if conversation_id is not None:
        if type(conversation_id) is not int or not 0 < conversation_id < 2**63:
            return None
        filters["conversation_id"] = conversation_id
    message = ConversationMessage.objects.select_related("conversation").filter(**filters).first()
    return message if message and message.external_id else None


def capture(connection, metadata, result, message):
    """Caller holds the authenticated connection lock after successful ingest."""
    window = _window(connection, metadata)
    if window is None or not isinstance(result, str) or result not in {"created", "duplicate"}:
        return False
    persisted = _message(connection, getattr(message, "pk", None))
    if persisted is None:
        return False
    evidence, generation, actor, _ = window
    chain = {"version": 1, "generation": generation, "actor_id": actor.pk,
             "received_at": timezone.now().isoformat(), "event_result": result,
             "conversation_id": persisted.conversation_id, "message_id": persisted.pk}
    from .avito_services_workplace_setup import WEBHOOK_KEY
    metadata[WEBHOOK_KEY] = {**evidence, CHAIN_KEY: chain}
    return True


def _owner_state(connection, message, actor):
    from .communication_services import users_with_conversation_access
    from .avito_deferred_messages import KEY
    if not users_with_conversation_access(connection.channel.organization).filter(pk=actor.pk).exists():
        return "not_eligible", None
    notifications = Notification.objects.filter(
        user=actor, organization_id=connection.channel.organization_id, kind="communication")
    if notifications.filter(dedupe_key=f"communication:{message.pk}").exists():
        return "direct_recorded", None
    # Existing quiet digests cover a durable message range. Read at most twenty
    # recent records; an older/unprovable link remains explicitly unconfirmed.
    prefix = f"avito-night:{connection.pk}:{actor.pk}:"
    for key in notifications.filter(dedupe_key__startswith=prefix).order_by("-pk").values_list("dedupe_key", flat=True)[:20]:
        match = re.fullmatch(re.escape(prefix) + r"([0-9]{1,20}):([0-9]{1,20})", key or "")
        if match and int(match[1]) <= message.pk <= int(match[2]):
            return "quiet_digest_recorded", None
    queued = connection.settings.get(KEY) if isinstance(connection.settings, dict) else None
    if (isinstance(queued, dict) and queued.get("actor_id") == actor.pk
            and queued.get("organization_id") == connection.channel.organization_id
            and queued.get("channel_id") == connection.channel_id
            and queued.get("account_id") == str(connection.external_id)
            and type(queued.get("first_id")) is int and type(queued.get("last_id")) is int
            and 0 < queued["first_id"] <= message.pk <= queued["last_id"]):
        error = queued.get("last_error_code")
        return "queued", error if isinstance(error, str) and error in QUEUE_ERRORS else "queue_error_unknown" if error else None
    return "not_recorded", None


def summary(connection):
    """Read-only fixed projection; raw messages, identities and hashes stay private."""
    result = {"source": SOURCE, "state": "binding_unverified", "generation_bound": False,
              "authenticated_event": False, "conversation_bound": False, "message_persisted": False,
              "event_result": None, "received_at": None, "owner_notification_state": "not_checked",
              "owner_notification_recorded": False, "owner_notification_error": None,
              "push_delivery_confirmed": False}
    window = _window(connection)
    if window is None:
        return result
    evidence, generation, actor, verified = window
    chain = evidence.get(CHAIN_KEY)
    if not isinstance(chain, dict) or chain.get("generation") != generation:
        return {**result, "state": "awaiting_first", "owner_notification_state": "awaiting_first"}
    from .avito_runtime import when
    received = when(chain.get("received_at"))
    if (type(chain.get("version")) is not int or chain.get("version") != 1
            or type(chain.get("actor_id")) is not int or chain.get("actor_id") != actor.pk
            or not isinstance(chain.get("event_result"), str) or chain.get("event_result") not in {"created", "duplicate"}
            or received is None or not verified <= received <= timezone.now()):
        return {**result, "state": "proof_invalid"}
    result.update(generation_bound=True, authenticated_event=True, event_result=chain["event_result"],
                  received_at=received.isoformat())
    message = _message(connection, chain.get("message_id"), chain.get("conversation_id"))
    if message is None:
        return {**result, "state": "persisted_row_missing"}
    owner_state, error = _owner_state(connection, message, actor)
    return {**result, "state": "accepted_persisted", "conversation_bound": True, "message_persisted": True,
            "owner_notification_state": owner_state,
            "owner_notification_recorded": owner_state in {"direct_recorded", "quiet_digest_recorded"},
            "owner_notification_error": error}


def display(connection):
    result = summary(connection)
    return {**result, "state_label": STATE_LABELS[result["state"]],
            "owner_notification_label": OWNER_LABELS[result["owner_notification_state"]]}
