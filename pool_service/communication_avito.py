import json
from dataclasses import dataclass
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone

from pool_service.communication_models import AvitoCredential, ConversationMessage
from pool_service.communication_secrets import decrypt_secret, encrypt_secret
from pool_service.communication_secrets import CommunicationSecretError
from pool_service.communication_services import receive_message


AVITO_API_ROOT = "https://api.avito.ru"


class AvitoError(Exception):
    """A sanitized provider error safe to persist and show to operators."""


class AvitoRetryableError(AvitoError):
    """A pre-send provider failure that is safe to retry within a bound."""


class AvitoAmbiguousDeliveryError(AvitoError):
    """The request may have reached Avito, so automatic retry is unsafe."""


@dataclass(frozen=True)
class AvitoInboundMessage:
    message_id: str
    chat_id: str
    author_id: str
    body: str
    participant_name: str


def parse_webhook(payload, account_id):
    """Validate the documented Avito Messenger v3 message webhook shape."""
    if not isinstance(payload, dict) or not isinstance(payload.get("payload"), dict):
        return None
    if payload["payload"].get("type") != "message":
        return None
    value = payload.get("payload", {}).get("value")
    if not isinstance(value, dict) or str(value.get("user_id", "")) != str(account_id):
        return None
    content = value.get("content")
    if not isinstance(content, dict):
        raise AvitoError("invalid_message_content")
    message_type = value.get("type")
    if message_type == "text":
        body = content.get("text", "")
    elif message_type in {"image", "item", "link", "location", "voice"}:
        body = content.get("text") or f"[{message_type}]"
    else:
        raise AvitoError("unsupported_message_type")
    required = (value.get("id"), value.get("chat_id"), value.get("author_id"), body)
    if not all(isinstance(item, (str, int)) and str(item).strip() for item in required):
        raise AvitoError("invalid_message")
    identifiers = [str(value[name]) for name in ("id", "chat_id", "author_id")]
    if any(len(identifier) > 255 for identifier in identifiers):
        raise AvitoError("provider_identifier_too_long")
    return AvitoInboundMessage(
        message_id=identifiers[0],
        chat_id=identifiers[1],
        author_id=identifiers[2],
        body=str(body)[:10000],
        participant_name=str(value.get("author_name") or value["author_id"])[:255],
    )


def ingest_webhook(connection, payload):
    incoming = parse_webhook(payload, connection.external_id)
    if incoming is None:
        return None, False
    # Avito also delivers messages authored by the connected account. They are
    # already represented by the local outbox and must not become inbound copies.
    if incoming.author_id == str(connection.external_id):
        return None, False
    return receive_message(
        connection=connection,
        external_conversation_id=incoming.chat_id,
        participant_name=incoming.participant_name,
        body=incoming.body,
        external_message_id=incoming.message_id,
    )


def _json_request(url, *, method="GET", headers=None, data=None, timeout=15, ambiguous_transport=False):
    request = Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(1024 * 1024 + 1)
    except HTTPError as exc:
        if not ambiguous_transport and (exc.code == 429 or 500 <= exc.code <= 599):
            raise AvitoRetryableError(f"provider_http_{exc.code}") from exc
        raise AvitoError(f"provider_http_{exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        if ambiguous_transport:
            raise AvitoAmbiguousDeliveryError("provider_delivery_unknown") from exc
        raise AvitoRetryableError("provider_unavailable") from exc
    if len(raw) > 1024 * 1024:
        raise AvitoError("provider_response_too_large")
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AvitoError("provider_invalid_json") from exc
    if not isinstance(value, dict):
        raise AvitoError("provider_invalid_response")
    return value


def access_token(connection):
    try:
        credential = AvitoCredential.objects.get(connection=connection)
    except AvitoCredential.DoesNotExist as exc:
        raise AvitoError("provider_credentials_missing") from exc
    now = timezone.now()
    if credential.access_token_encrypted and credential.access_token_expires_at and credential.access_token_expires_at > now + timedelta(seconds=60):
        try:
            return decrypt_secret(credential.access_token_encrypted)
        except CommunicationSecretError:
            # A stale token encrypted with an old key can be replaced using the
            # long-lived credentials instead of aborting the whole outbox batch.
            pass
    try:
        form = urlencode({
            "grant_type": "client_credentials",
            "client_id": decrypt_secret(credential.client_id_encrypted),
            "client_secret": decrypt_secret(credential.client_secret_encrypted),
        }).encode("ascii")
    except (CommunicationSecretError, UnicodeEncodeError) as exc:
        raise AvitoError("provider_credentials_invalid") from exc
    response = _json_request(
        f"{getattr(settings, 'AVITO_API_ROOT', AVITO_API_ROOT).rstrip('/')}/token/",
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        data=form,
    )
    token = response.get("access_token")
    if not isinstance(token, str) or not token:
        raise AvitoError("provider_token_missing")
    try:
        expires_in = max(120, min(int(response.get("expires_in", 86400)), 172800))
    except (TypeError, ValueError):
        expires_in = 86400
    credential.access_token_encrypted = encrypt_secret(token)
    credential.access_token_expires_at = now + timedelta(seconds=expires_in)
    credential.save(update_fields=["access_token_encrypted", "access_token_expires_at", "updated_at"])
    return token


def _provider_root():
    return getattr(settings, "AVITO_API_ROOT", AVITO_API_ROOT).rstrip("/")


def _webhook_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise AvitoError("provider_webhook_url_invalid")
    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise AvitoError("provider_webhook_url_invalid") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise AvitoError("provider_webhook_url_invalid")
    return value


def webhook_subscriptions(connection):
    token = access_token(connection)
    response = _json_request(
        f"{_provider_root()}/messenger/v1/subscriptions",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        data=b"",
    )
    subscriptions = response.get("subscriptions")
    if not isinstance(subscriptions, list):
        raise AvitoError("provider_subscriptions_invalid")
    urls = []
    for item in subscriptions:
        if not isinstance(item, dict):
            continue
        value = item.get("url")
        if isinstance(value, str) and 0 < len(value) <= 2048:
            urls.append(value)
    return urls


def subscribe_webhook(connection, url):
    url = _webhook_url(url)
    token = access_token(connection)
    response = _json_request(
        f"{_provider_root()}/messenger/v3/webhook",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        data=json.dumps({"url": url}).encode("utf-8"),
    )
    if response.get("ok") is not True:
        raise AvitoError("provider_webhook_rejected")
    return response


def unsubscribe_webhook(connection, url):
    url = _webhook_url(url)
    token = access_token(connection)
    response = _json_request(
        f"{_provider_root()}/messenger/v1/webhook/unsubscribe",
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        data=json.dumps({"url": url}).encode("utf-8"),
    )
    if response.get("ok") is not True:
        raise AvitoError("provider_webhook_unsubscribe_rejected")
    return response


def send_message(message):
    connection = message.conversation.connection
    if message.attachments.exists():
        raise AvitoError("attachments_require_provider_upload")
    if not message.body.strip():
        raise AvitoError("empty_message")
    token = access_token(connection)
    root = getattr(settings, "AVITO_API_ROOT", AVITO_API_ROOT).rstrip("/")
    account_id = quote(str(connection.external_id), safe="")
    chat_id = quote(str(message.conversation.external_id), safe="")
    response = _json_request(
        f"{root}/messenger/v1/accounts/{account_id}/chats/{chat_id}/messages",
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"},
        data=json.dumps({"message": {"text": message.body}, "type": "text"}).encode("utf-8"),
        ambiguous_transport=True,
    )
    provider_id = response.get("id")
    if not isinstance(provider_id, str) or not provider_id:
        raise AvitoError("provider_message_id_missing")
    if len(provider_id) > 255:
        raise AvitoError("provider_identifier_too_long")
    message.external_id = provider_id
    message.delivery_status = ConversationMessage.DELIVERY_DELIVERED
    message.delivered_at = timezone.now()
    message.delivery_error = ""
    message.save(update_fields=["external_id", "delivery_status", "delivered_at", "delivery_error"])
    return message
