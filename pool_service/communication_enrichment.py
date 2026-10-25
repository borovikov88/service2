"""Bounded Avito profile reads outside webhook requests and DB transactions."""
from datetime import timedelta
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .communication_clients import capture_client
from .communication_contact_data import avito_url, clean_listing, text
from .communication_models import ChannelConnection, Conversation
from .models import Organization

logger = logging.getLogger(__name__)


def chat_context(chat, account_id, participant_id=""):
    if not isinstance(chat, dict):
        return {}
    users = chat.get("users") if isinstance(chat.get("users"), list) else []
    peers = []
    for user in users[:20]:
        if not isinstance(user, dict):
            continue
        profile = user.get("public_user_profile")
        profile = profile if isinstance(profile, dict) else {}
        value = profile.get("user_id") or user.get("id") or user.get("user_id")
        if type(value) not in (str, int):
            continue
        identity = str(value).strip()
        if not identity or len(identity) > 255 or identity == str(account_id):
            continue
        if participant_id and identity != str(participant_id):
            continue
        peers.append({"id": identity, "name": text(user.get("name"), 255), "url": avito_url(profile.get("url"))})
    result = {"participant": peers[0]} if len(peers) == 1 else {}
    context = chat.get("context")
    value = context.get("value") if isinstance(context, dict) and context.get("type") == "item" else None
    if isinstance(value, dict):
        images = value.get("images") if isinstance(value.get("images"), dict) else {}
        main = images.get("main") if isinstance(images.get("main"), dict) else {}
        image = next((url for size, url in sorted(main.items()) if isinstance(url, str) and avito_url(url, image=True)), "")
        location = value.get("location") if isinstance(value.get("location"), dict) else {}
        result["item"] = clean_listing({"id": str(value.get("id") or "")[:80],
            "title": value.get("title"), "url": value.get("url"), "image_url": image,
            "price": value.get("price_string"), "location": location.get("title")})
    return result


def apply_chat_context_locked(conversation, context):
    """Apply only whitelisted fields; caller has checked the current binding."""
    previous = dict(conversation.provider_context) if isinstance(conversation.provider_context, dict) else {}
    participant = context.get("participant") if isinstance(context, dict) else None
    if isinstance(participant, dict):
        identity = text(participant.get("id"), 255)
        if identity and identity != str(conversation.connection.external_id):
            if conversation.participant_external_id and conversation.participant_external_id != identity:
                raise ValueError("conversation_participant_mismatch")
            conversation.participant_external_id = identity
            name = text(participant.get("name"), 255)
            if name and not name.isdigit():
                conversation.participant_name = name
            previous["participant"] = {"id": identity, "name": name, "url": avito_url(participant.get("url"))}
    item = clean_listing(context.get("item")) if isinstance(context, dict) else {}
    if item:
        previous["item"] = item
    conversation.provider_context = previous
    conversation.context_synced_at = timezone.now()
    conversation.save(update_fields=["provider_context", "participant_name", "participant_external_id", "context_synced_at", "updated_at"])


def _snapshot(conversation):
    connection = conversation.connection
    credential = getattr(connection, "avito_credential", None)
    credential_snapshot = (credential.client_id_encrypted, credential.client_secret_encrypted) if credential else None
    return (conversation.connection_id, conversation.organization_id, conversation.external_id, conversation.client_id,
            conversation.participant_external_id, connection.channel_id, connection.channel.organization_id,
            str(connection.external_id), connection.api_token_hash, connection.channel.kind,
            connection.is_active, connection.channel.is_active, conversation.context_attempted_at, credential_snapshot)


def _lock(conversation_id):
    reference = Conversation.objects.only("connection_id", "organization_id").get(pk=conversation_id)
    connection = ChannelConnection.objects.select_for_update().select_related("channel").get(pk=reference.connection_id)
    Organization.objects.select_for_update().get(pk=reference.organization_id)
    conversation = Conversation.objects.select_for_update().select_related("connection__channel").get(pk=conversation_id)
    if (conversation.connection_id != connection.pk or conversation.organization_id != reference.organization_id
            or conversation.organization_id != connection.channel.organization_id
            or not connection.is_active or not connection.channel.is_active):
        raise ValueError("communication_connection_binding_changed")
    conversation.connection = connection
    return conversation


def _capture_history_locked(conversation, history):
    for message in reversed(history):
        capture_client(conversation, body=message.body, message_id=message.pk)
    conversation.contact_data["history_captured"] = True
    conversation.save(update_fields=["contact_data"])


def enrich_conversation(conversation_id, *, load_history=True, claim_time=None, refresh_context=True):
    from .communication_avito import AvitoError, _json_request, _provider_root, access_token, authorized_account_id
    from urllib.parse import quote
    with transaction.atomic():
        conversation = _lock(conversation_id)
        if claim_time is not None and conversation.context_attempted_at != claim_time:
            return "deferred"
        state = conversation.contact_data if isinstance(conversation.contact_data, dict) else {}
        needs_history = load_history and not state.get("history_captured")
        history = []
        if needs_history:
            history = list(conversation.messages.filter(direction="in").order_by("-created_at", "-pk")[:100])
        identity_body = "\n".join(message.body for message in history)[:10000] if not conversation.client_id else ""
        unknown_avito = conversation.connection.channel.kind == "avito" and not conversation.participant_external_id
        customer = capture_client(conversation, body=identity_body, create=not unknown_avito or bool(conversation.client_id))
        if customer and needs_history:
            _capture_history_locked(conversation, history)
        if not refresh_context or conversation.connection.channel.kind != "avito":
            return "captured"
        now = timezone.now()
        if conversation.context_synced_at and conversation.context_synced_at > now - timedelta(hours=24):
            return "cached"
        if claim_time is None and conversation.context_attempted_at and conversation.context_attempted_at > now - timedelta(minutes=5):
            return "deferred"
        if claim_time is None:
            conversation.context_attempted_at = now
            conversation.save(update_fields=["context_attempted_at"])
        snapshot, connection = _snapshot(conversation), conversation.connection
        chat_id, participant_id = conversation.external_id, conversation.participant_external_id
    # No network request or provider-derived URL is executed under row locks.
    account_id = authorized_account_id(connection)
    if str(account_id) != str(connection.external_id):
        raise AvitoError("provider_account_mismatch")
    raw = _json_request(
        f"{_provider_root()}/messenger/v2/accounts/{quote(str(account_id), safe='')}/chats/{quote(chat_id, safe='')}",
        headers={"Authorization": f"Bearer {access_token(connection)}", "Accept": "application/json"}, timeout=10,
    )
    if str(raw.get("id", "")) != chat_id:
        raise AvitoError("provider_chat_mismatch")
    context = chat_context(raw, account_id, participant_id)
    with transaction.atomic():
        current = _lock(conversation_id)
        if _snapshot(current) != snapshot:
            return "binding_changed"
        apply_chat_context_locked(current, context)
        customer = capture_client(current, body=identity_body,
            create=bool(current.client_id or current.participant_external_id))
        if customer and needs_history:
            _capture_history_locked(current, history)
    return "enriched"


def _reap(process):
    try:
        process.wait(timeout=240)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def start_client_enrichment_worker(conversation_ids):
    if not getattr(settings, "COMMUNICATION_CLIENT_ENRICHMENT_ENABLED", True):
        return False
    requested = list(dict.fromkeys(int(value) for value in conversation_ids if str(value).isdigit()))[:20]
    claim_time = timezone.now()
    ids = []
    for conversation_id in requested:
        eligible = Conversation.objects.filter(pk=conversation_id, connection__is_active=True, connection__channel__is_active=True).filter(
            Q(client__isnull=True) | Q(connection__channel__kind="avito", context_synced_at__isnull=True)
            | Q(connection__channel__kind="avito", context_synced_at__lt=claim_time - timedelta(hours=24)),
        ).filter(Q(context_attempted_at__isnull=True) | Q(context_attempted_at__lt=claim_time - timedelta(minutes=5)))
        if eligible.update(context_attempted_at=claim_time):
            ids.append(conversation_id)
    if not ids:
        return False
    base = Path(settings.BASE_DIR)
    production_python = base.parent / "venv/bin/python"
    executable = str(production_python) if production_python.is_file() else sys.executable
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    try:
        process = subprocess.Popen([executable, str(base / "manage.py"), "enrich_communication_clients", "--ids", ",".join(map(str, ids)), "--claim-time", claim_time.isoformat()],
            cwd=str(base), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **options)
    except OSError:
        Conversation.objects.filter(pk__in=ids, context_attempted_at=claim_time).update(context_attempted_at=None)
        logger.warning("Communication client enrichment worker could not start")
        return False
    threading.Thread(target=_reap, args=(process,), daemon=True, name="communication-client-enrichment-reaper").start()
    return True
