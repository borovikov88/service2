from datetime import timedelta
import re
import socket
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.files import File
from django.db.models import F, Q
from django.utils import timezone

from pool_service.communication_models import PhoneCall


class RecordingDownloadError(Exception):
    pass


def _recording_host_allowed(call, hostname):
    host = (hostname or "").strip().lower()
    if not host:
        return False
    configured = {
        str(item).strip().lower()
        for item in (call.connection.recording_allowed_hosts or [])
        if isinstance(item, str) and item.strip()
    }
    return (
        host in configured
        or host == "megapbx.ru"
        or host.endswith(".megapbx.ru")
    )


def _validate_recording_url(call, value):
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise RecordingDownloadError("invalid_recording_url") from exc
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or not _recording_host_allowed(call, hostname)
    ):
        raise RecordingDownloadError("untrusted_recording_url")
    return parsed


class _SafeRecordingRedirectHandler(HTTPRedirectHandler):
    def __init__(self, call):
        super().__init__()
        self.call = call

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_recording_url(self.call, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_recording(call, timeout):
    _validate_recording_url(call, call.recording_ref)
    request = Request(
        call.recording_ref,
        headers={
            "User-Agent": "Service2-Call-Recording/1.0",
            "Accept": "audio/mpeg,audio/*;q=0.9,application/octet-stream;q=0.8",
        },
        method="GET",
    )
    opener = build_opener(_SafeRecordingRedirectHandler(call))
    return opener.open(request, timeout=timeout)


def _content_type(response):
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    get_content_type = getattr(headers, "get_content_type", None)
    if callable(get_content_type):
        return (get_content_type() or "").lower()
    raw = headers.get("Content-Type", "")
    return raw.split(";", 1)[0].strip().lower()


def _content_length(response):
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Content-Length")
    if not raw:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _looks_like_mp3(prefix):
    if prefix.startswith(b"ID3"):
        return True
    return len(prefix) >= 2 and prefix[0] == 0xFF and (prefix[1] & 0xE0) == 0xE0


def _safe_filename(call):
    raw = re.sub(r"[^A-Za-z0-9._-]+", "_", call.external_id or "call").strip("._")
    if not raw:
        raw = "call"
    return f"call_{call.pk}_{raw[:80]}.mp3"


def _mark_failed(call_id, code):
    PhoneCall.objects.filter(pk=call_id).update(
        recording_status=PhoneCall.RECORDING_FAILED,
        recording_error=str(code)[:500],
    )


def _claim_recording(call_id, *, force=False):
    now = timezone.now()
    stale_before = now - timedelta(minutes=30)
    queryset = PhoneCall.objects.filter(pk=call_id, recording_file="")
    if not force:
        max_attempts = int(
            getattr(settings, "COMMUNICATION_RECORDING_MAX_ATTEMPTS", 20)
        )
        queryset = queryset.filter(recording_attempts__lt=max_attempts).filter(
            Q(
                recording_status__in=[
                    PhoneCall.RECORDING_NONE,
                    PhoneCall.RECORDING_PENDING,
                    PhoneCall.RECORDING_FAILED,
                ]
            )
            | Q(
                recording_status=PhoneCall.RECORDING_DOWNLOADING,
                recording_last_attempt_at__lt=stale_before,
            )
            | Q(
                recording_status=PhoneCall.RECORDING_DOWNLOADING,
                recording_last_attempt_at__isnull=True,
            )
        )
    updated = queryset.update(
        recording_status=PhoneCall.RECORDING_DOWNLOADING,
        recording_error="",
        recording_attempts=F("recording_attempts") + 1,
        recording_last_attempt_at=now,
    )
    return bool(updated)


def download_call_recording(call_id, *, force=False):
    call = PhoneCall.objects.select_related("connection").filter(pk=call_id).first()
    if call is None:
        return False
    if call.recording_file and not force:
        if call.recording_status != PhoneCall.RECORDING_STORED:
            PhoneCall.objects.filter(pk=call.pk).update(
                recording_status=PhoneCall.RECORDING_STORED,
                recording_error="",
            )
        return True
    if not call.recording_ref:
        PhoneCall.objects.filter(pk=call.pk).update(
            recording_status=PhoneCall.RECORDING_NONE,
            recording_error="",
        )
        return False
    if not _claim_recording(call.pk, force=force):
        return False

    call.refresh_from_db()
    timeout = float(getattr(settings, "COMMUNICATION_RECORDING_DOWNLOAD_TIMEOUT_SECONDS", 10))
    max_bytes = int(getattr(settings, "COMMUNICATION_RECORDING_MAX_BYTES", 50 * 1024 * 1024))

    try:
        with _open_recording(call, timeout) as response:
            declared_length = _content_length(response)
            if declared_length is not None and declared_length > max_bytes:
                raise RecordingDownloadError("recording_too_large")

            content_type = _content_type(response)
            if content_type.startswith("text/") or content_type in {
                "application/json",
                "application/xml",
                "text/html",
            }:
                raise RecordingDownloadError(f"unexpected_content_type:{content_type or 'unknown'}")

            total = 0
            prefix = b""
            with tempfile.SpooledTemporaryFile(max_size=5 * 1024 * 1024, mode="w+b") as spool:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    if not prefix:
                        prefix = chunk[:16]
                    total += len(chunk)
                    if total > max_bytes:
                        raise RecordingDownloadError("recording_too_large")
                    spool.write(chunk)

                if total == 0:
                    raise RecordingDownloadError("empty_recording")
                if not content_type.startswith("audio/") and not _looks_like_mp3(prefix):
                    raise RecordingDownloadError(
                        f"unexpected_content_type:{content_type or 'unknown'}"
                    )

                spool.seek(0)
                call.refresh_from_db()
                call.recording_file.save(
                    _safe_filename(call),
                    File(spool),
                    save=False,
                )
                call.recording_status = PhoneCall.RECORDING_STORED
                call.recording_error = ""
                call.recording_downloaded_at = timezone.now()
                call.save(
                    update_fields=[
                        "recording_file",
                        "recording_status",
                        "recording_error",
                        "recording_downloaded_at",
                    ]
                )
        return True
    except HTTPError as exc:
        _mark_failed(call.pk, f"provider_http_{exc.code}")
    except (URLError, socket.timeout, TimeoutError):
        _mark_failed(call.pk, "provider_unavailable")
    except RecordingDownloadError as exc:
        _mark_failed(call.pk, str(exc))
    except OSError:
        _mark_failed(call.pk, "recording_storage_error")
    return False
