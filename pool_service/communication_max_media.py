"""Bounded transfers between private storage and MAX's signed media URLs."""

import hashlib
import json
import re
from http.client import HTTPException, HTTPSConnection
from tempfile import SpooledTemporaryFile
from time import monotonic
from urllib.parse import quote, urljoin, urlsplit
from uuid import uuid4

from django.core.files import File
from django.db import transaction
from django.views.decorators.debug import sensitive_variables

from pool_service.communication_models import MessageAttachment
from pool_service.communication_secrets import CommunicationSecretError, decrypt_secret


MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
MEDIA_NAMES = {
    "image": "Фото MAX.jpg",
    "audio": "Аудио MAX.ogg",
    "video": "Видео MAX.mp4",
    "file": "Файл MAX",
    "sticker": "Стикер MAX.webp",
}
# MAX generates the signed CDN URLs. Never send the bot's API token to them.
MEDIA_DOMAINS = (
    "max.ru", "cdn-max.ru", "oneme.ru", "okcdn.ru", "mycdn.me", "userapi.com", "vkuserphoto.ru",
)
INLINE_TYPES = {
    "image": {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"},
    "sticker": {"image/jpeg", "image/png", "image/gif", "image/webp"},
    "audio": {"audio/mpeg", "audio/mp4", "audio/ogg", "audio/wav", "audio/x-wav", "audio/aac", "audio/webm"},
    "video": {"video/mp4", "video/webm", "video/quicktime"},
}
MEDIA_KEY = re.compile(r"(?!\.\.?$)[a-zA-Z0-9_.-]{1,512}")


class MaxMediaError(Exception):
    """A sanitized error that never contains a URL, credential, or file name."""


def media_kind(content_type):
    prefix = content_type.partition("/")[0].lower()
    return prefix if prefix in {"image", "video", "audio"} else "file"


def media_name(value, kind):
    if isinstance(value, str):
        filename = value.replace("\\", "/").rsplit("/", 1)[-1]
        filename = "".join(char for char in filename if char.isprintable() and char not in '"<>')
        if filename.strip(" ."):
            return filename[-200:]
    return MEDIA_NAMES[kind]


def media_fingerprints(kind, payload):
    """A stable provider ID is preferred over an exact signed URL."""
    if not isinstance(payload, dict):
        return ()
    identifiers = []
    photo_id = payload.get("photo_id")
    if kind == "image" and type(photo_id) is int and photo_id > 0:
        identifiers.append(("photo_id", str(photo_id)))
    sticker = payload.get("code")
    if kind == "sticker" and isinstance(sticker, str) and 1 <= len(sticker) <= 4096:
        identifiers.append(("code", sticker))
    token = payload.get("token")
    if isinstance(token, str) and 1 <= len(token) <= 4096:
        identifiers.append(("token", token))
    try:
        host, target = media_url(payload.get("url"))
    except MaxMediaError:
        pass
    else:
        # Some CDN paths are shared, with the asset ID present only in the query.
        identifiers.append(("url", host + target))
    hashes = []
    for key, value in identifiers:
        try:
            encoded = (kind + ":" + key + ":" + value).encode("utf-8")
        except UnicodeError:
            continue
        hashes.append(hashlib.sha256(encoded).hexdigest())
    return tuple(hashes)


def media_url(url):
    if (
        not isinstance(url, str) or len(url) > 8192
        or any(ord(char) < 33 or ord(char) > 126 for char in url) or "\\" in url
    ):
        raise MaxMediaError("max_media_url_invalid")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        if (
            parts.scheme != "https" or not host or parts.username or parts.password
            or "@" in parts.netloc or parts.port not in (None, 443) or parts.fragment
            or not any(host == suffix or host.endswith("." + suffix) for suffix in MEDIA_DOMAINS)
        ):
            raise MaxMediaError("max_media_url_invalid")
    except ValueError:
        raise MaxMediaError("max_media_url_invalid") from None
    target = parts.path or "/"
    return host, target + ("?" + parts.query if parts.query else "")


@sensitive_variables("url", "target", "content")
def _upload(url, attachment, kind):
    from pool_service.communication_max_outbound import max_api_ssl_context

    host, target = media_url(url)
    boundary = "rovik-" + uuid4().hex
    # Generate safe multipart metadata independently of the source MIME header.
    name = media_name(attachment.original_name, kind)
    safe_name = re.sub(r"[^\w.() -]", "_", name)
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="data"; filename="{safe_name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("ascii")
    connection = HTTPSConnection(host, timeout=15, context=max_api_ssl_context())
    try:
        with attachment.original.open("rb") as source:
            content = source.read(MAX_UPLOAD_BYTES + 1)
        if not 1 <= len(content) <= MAX_UPLOAD_BYTES:
            raise MaxMediaError("max_upload_size")
        connection.request(
            "POST", target, body=head + content + tail,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with connection.getresponse() as response:
            if response.status != 200:
                raise MaxMediaError("max_upload_failed")
            return response.read(65537)
    except (OSError, HTTPException, ValueError):
        raise MaxMediaError("max_upload_failed") from None
    finally:
        connection.close()


@sensitive_variables("token", "endpoint", "result", "raw", "upload_token")
def upload_attachment(token, attachment):
    from pool_service.communication_max_outbound import _request

    kind = attachment.max_media_kind
    if kind not in {"image", "video", "audio", "file"}:
        raise MaxMediaError("max_upload_failed")
    endpoint = _request("POST", f"/uploads?type={kind}", token)
    if not isinstance(endpoint, dict):
        raise MaxMediaError("max_upload_failed")
    raw = _upload(endpoint.get("url"), attachment, kind)
    if kind in {"audio", "video"}:
        upload_token = endpoint.get("token")
    else:
        try:
            result = json.loads(raw) if len(raw) <= 65536 else None
        except (ValueError, UnicodeError):
            raise MaxMediaError("max_upload_failed") from None
        if not isinstance(result, dict):
            raise MaxMediaError("max_upload_failed")
        if kind == "image":
            photos = result.get("photos")
            first = next(iter(photos.values()), None) if isinstance(photos, dict) else None
            upload_token = first.get("token") if isinstance(first, dict) else None
        else:
            upload_token = result.get("token")
    if not isinstance(upload_token, str) or not 1 <= len(upload_token) <= 4096:
        raise MaxMediaError("max_upload_failed")
    return {"type": kind, "payload": {"token": upload_token}}


@sensitive_variables("token", "source", "payload", "video_info", "video_urls")
def _remote_url(attachment, token):
    from pool_service.communication_max_outbound import _request

    message = attachment.message
    conversation = message.conversation
    mid = message.external_id
    if not isinstance(mid, str) or not MEDIA_KEY.fullmatch(mid):
        raise MaxMediaError("max_media_unavailable")
    source = _request("GET", "/messages/" + quote(mid, safe=""), token)
    if not isinstance(source, dict):
        raise MaxMediaError("max_media_unavailable")
    sender, recipient, body = (source.get(key) for key in ("sender", "recipient", "body"))
    if (
        not isinstance(sender, dict) or str(sender.get("user_id")) != conversation.participant_external_id
        or not isinstance(recipient, dict) or recipient.get("chat_type") != "dialog"
        or (recipient.get("user_id") is not None and str(recipient["user_id"]) != conversation.connection.external_id)
        or not isinstance(body, dict) or body.get("mid") != mid
    ):
        raise MaxMediaError("max_media_unavailable")
    entries = body.get("attachments")
    index = attachment.max_media_position
    if not isinstance(entries, list) or index is None or index >= len(entries):
        raise MaxMediaError("max_media_unavailable")
    entry = entries[index]
    if not isinstance(entry, dict) or entry.get("type") != attachment.max_media_kind:
        raise MaxMediaError("max_media_unavailable")
    payload = entry.get("payload")
    if (
        not attachment.max_media_fingerprint
        or attachment.max_media_fingerprint not in media_fingerprints(attachment.max_media_kind, payload)
    ):
        raise MaxMediaError("max_media_unavailable")
    video_token = payload.get("token")
    if attachment.max_media_kind == "video" and isinstance(video_token, str) and MEDIA_KEY.fullmatch(video_token):
        video_info = _request("GET", "/videos/" + quote(video_token, safe=""), token)
        video_urls = video_info.get("urls") if isinstance(video_info, dict) else None
        if isinstance(video_urls, dict):
            for quality in ("mp4_480", "mp4_360", "mp4_240", "mp4_144", "mp4_720", "mp4_1080"):
                if video_urls.get(quality):
                    return video_urls[quality]
    return payload.get("url")


@sensitive_variables("url", "target", "chunk")
def _download(url, destination, kind):
    from pool_service.communication_max_outbound import max_api_ssl_context

    deadline = monotonic() + 20
    for _ in range(3):
        host, target = media_url(url)
        connection = HTTPSConnection(host, timeout=8, context=max_api_ssl_context())
        try:
            connection.request("GET", target, headers={"Accept-Encoding": "identity"})
            with connection.getresponse() as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.getheader("Location")
                    if not location:
                        raise MaxMediaError("max_media_unavailable")
                    url = urljoin(url, location)
                    continue
                if response.status != 200:
                    raise MaxMediaError("max_media_unavailable")
                length = response.getheader("Content-Length")
                if length and int(length) > MAX_DOWNLOAD_BYTES:
                    raise MaxMediaError("max_media_size")
                size = 0
                while chunk := response.read1(65536):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise MaxMediaError("max_media_size")
                    if monotonic() > deadline:
                        raise MaxMediaError("max_media_unavailable")
                    destination.write(chunk)
                if not size or length and size != int(length):
                    raise MaxMediaError("max_media_unavailable")
                content_type = response.getheader("Content-Type", "").partition(";")[0].strip().lower()
                if content_type not in INLINE_TYPES.get(kind, set()):
                    content_type = "application/octet-stream"
                return size, content_type
        except (OSError, HTTPException, ValueError):
            raise MaxMediaError("max_media_unavailable") from None
        finally:
            connection.close()
    raise MaxMediaError("max_media_unavailable")


@sensitive_variables("token", "url")
def cache_incoming_media(attachment):
    """Retrieve a fresh link on first open and retain the file in private storage."""
    message = attachment.message
    conversation = message.conversation
    connection = conversation.connection
    if (
        not connection.is_active or not connection.channel.is_active or not connection.max_bot_token_encrypted
        or connection.channel.organization_id != conversation.organization_id
        or message.direction != "in"
    ):
        raise MaxMediaError("max_media_unavailable")
    try:
        token = decrypt_secret(connection.max_bot_token_encrypted)
    except CommunicationSecretError:
        raise MaxMediaError("max_media_unavailable") from None
    url = _remote_url(attachment, token)
    with SpooledTemporaryFile(max_size=1024 * 1024) as buffer:
        size, content_type = _download(url, buffer, attachment.max_media_kind)
        buffer.seek(0)
        with transaction.atomic():
            locked = MessageAttachment.objects.select_for_update().get(pk=attachment.pk)
            if not locked.original:
                # A stable local name is sufficient; the download uses original_name.
                suffix = "." + content_type.split("/")[-1] if content_type != "application/octet-stream" else ""
                locked.original.save("max-" + str(locked.pk) + suffix, File(buffer), save=False)
                locked.original_size = size
                locked.content_type = content_type
                locked.save(update_fields=["original", "original_size", "content_type"])
    return locked
